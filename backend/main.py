from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from contextlib import asynccontextmanager
from google.transit import gtfs_realtime_pb2
import httpx
import asyncio
import json
import time
import math
import concurrent.futures

# --- Configuration ---
FETCH_INTERVAL_VEHICLES = 1.0  
FETCH_INTERVAL_STOPS = 10.0
AGENCY_FILTER = "RET"

# Rotterdam Area Bounding Box
bbox_min_lat, bbox_max_lat = 51.5, 52.3
bbox_min_lon, bbox_max_lon = 3.8, 4.9

# --- Global State ---
current_vehicles = {} 
current_stops = {}
trip_headsigns = {} # Stores: trip_id -> "Slinge", "Den Haag Centraal", etc.

# Subscribers
vehicle_subscribers = set()
stop_subscribers = set()

# Thread Pool (Keeps the main loop fast)
process_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)

def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2.0)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(delta_lambda/2.0)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

# --- PARSING LOGIC (Run in separate threads) ---

def parse_vehicles(content, old_vehicles, trip_headsigns):
    """
    Parses vehicle positions.
    Crucially, it merges the 'Headsign' (Destination) we found in the other feed.
    """
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(content)

    updates = []
    new_state = {}
    current_time = time.time()

    for entity in feed.entity:
        if not entity.HasField('vehicle'): continue
        
        v_id = str(entity.id)
        
        # Broad filter: Check ID, Trip, Route for RET tag
        # Also include generic 'Metro' or 'Tram' if in bounding box
        search_str = (v_id + str(entity.vehicle.trip.route_id) + str(entity.vehicle.trip.trip_id)).upper()
        if "RET" not in search_str:
             # Fallback: If it's explicitly a Metro in Rotterdam, take it
             if "METRO" in search_str and bbox_min_lat <= entity.vehicle.position.latitude <= bbox_max_lat:
                 pass
             else:
                 continue

        v = entity.vehicle
        pos = v.position
        
        if not pos.latitude or not pos.longitude: continue
        if abs(pos.latitude) < 1: continue

        # Bounding Box
        if not (bbox_min_lat <= pos.latitude <= bbox_max_lat and bbox_min_lon <= pos.longitude <= bbox_max_lon):
            continue
            
        # --- Type & Line Logic ---
        route_id = v.trip.route_id.upper()
        v_type = "bus" # Default fallback
        
        # Heuristic to detect Metro vs Tram based on Route ID
        if "METRO" in route_id or route_id.startswith("RET:M"):
            v_type = "metro"
        elif "TRAM" in route_id or (len(route_id) > 4 and route_id[4:].isdigit() and int(route_id[4:]) < 30):
            v_type = "tram"
        
        # --- DESTINATION HUNTING ---
        # 1. Check if we already found the name in the TripUpdates feed (Best Source)
        headsign = trip_headsigns.get(v.trip.trip_id, "")
        
        # 2. If not, check if this feed has it explicitly
        if not headsign and v.trip.HasField('trip_headsign'):
            headsign = v.trip.trip_headsign
            
        # 3. If still empty, use a placeholder so frontend doesn't crash
        if not headsign:
            headsign = "Unknown"

        # Speed Calc
        speed = 0.0
        if v_id in old_vehicles:
            prev = old_vehicles[v_id]
            # Keep previous headsign if we lose it temporarily
            if headsign == "Unknown" and prev["headsign"] != "Unknown":
                headsign = prev["headsign"]
            
            ts_diff = v.timestamp - prev["timestamp"]
            if ts_diff > 0:
                dist = haversine_distance(prev["lat"], prev["lon"], pos.latitude, pos.longitude)
                speed = dist / ts_diff
            else:
                speed = prev["speed"]
        
        if pos.speed > 0: speed = pos.speed
        if speed > 36.0: speed = 0.0 

        # Clean Label (e.g., "RET:Metro:D" -> "D")
        label = v.vehicle.label if v.vehicle.label else v_id.split(':')[-1]
        
        data = {
            "id": v_id,
            "label": label,          # "D", "23", "E"
            "lat": round(pos.latitude, 6),
            "lon": round(pos.longitude, 6),
            "bearing": round(pos.bearing, 1),
            "speed": round(speed, 1),
            "trip_id": v.trip.trip_id,
            "route_id": v.trip.route_id,
            "headsign": headsign,    # "De Akkers", "Slinge", "Centraal Station"
            "type": v_type,          # "metro", "tram", "bus"
            "timestamp": int(v.timestamp if v.timestamp else current_time)
        }

        # Change Detection
        if v_id in old_vehicles:
            old = old_vehicles[v_id]
            if (old["lat"] != data["lat"] or old["lon"] != data["lon"]):
                updates.append(data)
        else:
            updates.append(data)

        new_state[v_id] = data

    return new_state, updates

def parse_stops(content, current_time):
    """
    Parses trip updates to find ETAs and, crucially, DESTINATIONS.
    """
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(content)
    
    new_stops = {}
    new_headsigns = {} # We collect destination names here
    stop_updates_list = []

    for entity in feed.entity:
        if not entity.HasField('trip_update'): continue
        tu = entity.trip_update
        
        if "RET" not in tu.trip.trip_id and "RET" not in tu.trip.route_id:
            continue

        trip_id = tu.trip.trip_id
        route_id = tu.trip.route_id
        
        # --- EXTRACT HEADSIGN (DESTINATION) ---
        found_headsign = None
        
        # Priority 1: Explicit Trip Header
        if tu.trip.HasField('trip_headsign'):
            found_headsign = tu.trip.trip_headsign
            
        # Priority 2: The headsign of the LAST stop in the list
        if not found_headsign and tu.stop_time_update:
            last_stop = tu.stop_time_update[-1]
            if last_stop.HasField('stop_headsign'):
                found_headsign = last_stop.stop_headsign
        
        if found_headsign:
            new_headsigns[trip_id] = found_headsign

        # --- Process Stops ---
        for stu in tu.stop_time_update:
            arrival_time = stu.arrival.time if stu.arrival.time else stu.departure.time
            
            # Filter valid times (Now-5m to Now+60m)
            if not (current_time - 300 < arrival_time < current_time + 3600):
                continue

            stop_id = stu.stop_id
            if stop_id not in new_stops:
                new_stops[stop_id] = {"id": stop_id, "buses": []}
            
            new_stops[stop_id]["buses"].append({
                "trip_id": trip_id,
                "route_id": route_id,
                "headsign": found_headsign if found_headsign else "Unknown",
                "time": arrival_time,
                "delay": stu.arrival.delay if stu.arrival.HasField('delay') else 0
            })

    # Sort and trim
    for s_id in new_stops:
        new_stops[s_id]["buses"].sort(key=lambda x: x["time"])
        new_stops[s_id]["buses"] = new_stops[s_id]["buses"][:5]
        stop_updates_list.append(new_stops[s_id])

    return new_stops, new_headsigns, stop_updates_list

# --- ASYNC WORKERS ---

async def vehicle_worker():
    global current_vehicles, trip_headsigns
    
    limits = httpx.Limits(max_keepalive_connections=5)
    async with httpx.AsyncClient(verify=False, limits=limits) as client:
        while True:
            start_time = time.time()
            try:
                # Use main NL feed
                resp = await client.get("http://gtfs.ovapi.nl/nl/vehiclePositions.pb", timeout=6.0)
                if resp.status_code == 200:
                    loop = asyncio.get_running_loop()
                    new_vehicles, updates = await loop.run_in_executor(
                        process_pool, parse_vehicles, resp.content, current_vehicles, trip_headsigns
                    )
                    
                    # Log status locally
                    if len(new_vehicles) > 0:
                        print(f"Tracking {len(new_vehicles)} RET vehicles.", end='\r')
                    
                    current_vehicles = new_vehicles
                    
                    if updates and vehicle_subscribers:
                        msg = json.dumps({"type": "vehicles", "data": updates})
                        for q in list(vehicle_subscribers):
                            if not q.full(): q.put_nowait(msg)

            except Exception as e:
                print(f"Vehicle fetch error: {e}")

            elapsed = time.time() - start_time
            await asyncio.sleep(max(0.5, FETCH_INTERVAL_VEHICLES - elapsed))

async def stop_worker():
    global current_stops, trip_headsigns
    
    limits = httpx.Limits(max_keepalive_connections=5)
    async with httpx.AsyncClient(verify=False, limits=limits, headers={"User-Agent": "RETTracker/2.0"}) as client:
        while True:
            start_time = time.time()
            try:
                resp = await client.get("http://gtfs.ovapi.nl/new/tripUpdates.pb", timeout=10.0)
                if resp.status_code == 200:
                    loop = asyncio.get_running_loop()
                    # Offload to thread
                    new_stops, new_headsigns, updates = await loop.run_in_executor(
                        process_pool, parse_stops, resp.content, start_time
                    )
                    
                    current_stops = new_stops
                    trip_headsigns.update(new_headsigns) # Sync destinations to global map
                    
                    if updates and stop_subscribers:
                        msg = json.dumps({"type": "stops", "data": updates})
                        for q in list(stop_subscribers):
                            if not q.full(): q.put_nowait(msg)

            except Exception as e:
                print(f"Stop fetch error: {e}")

            elapsed = time.time() - start_time
            await asyncio.sleep(max(1.0, FETCH_INTERVAL_STOPS - elapsed))

# --- APP LIFECYCLE ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    t1 = asyncio.create_task(vehicle_worker())
    t2 = asyncio.create_task(stop_worker())
    yield
    t1.cancel()
    t2.cancel()
    process_pool.shutdown()

app = FastAPI(lifespan=lifespan)

# --- CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def health_check():
    return {"status": "ok", "message": "Optimized Backend with Headsigns Running gooood"}

@app.get("/vehicles-sse")
async def vehicles_sse(request: Request):
    async def event_generator():
        q = asyncio.Queue(maxsize=100)
        vehicle_subscribers.add(q)
        try:
            if current_vehicles:
                yield f"data: {json.dumps({'type': 'vehicles', 'data': list(current_vehicles.values())})}\n\n"
            else:
                yield ": keep-alive\n\n"

            while True:
                if await request.is_disconnected(): break
                try:
                    data = await asyncio.wait_for(q.get(), timeout=5.0)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        except Exception:
            pass
        finally:
            vehicle_subscribers.discard(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )

@app.get("/stops-sse")
async def stops_sse(request: Request):
    async def event_generator():
        q = asyncio.Queue(maxsize=50)
        stop_subscribers.add(q)
        try:
            if current_stops:
                all_stops = list(current_stops.values())
                # Send in small chunks
                for i in range(0, len(all_stops), 50):
                    yield f"data: {json.dumps({'type': 'stops', 'data': all_stops[i:i+50]})}\n\n"
                    await asyncio.sleep(0.01)
            
            while True:
                if await request.is_disconnected(): break
                try:
                    data = await asyncio.wait_for(q.get(), timeout=5.0)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        except Exception:
            pass
        finally:
            stop_subscribers.discard(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )