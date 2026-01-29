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
# We use the General NL endpoint bc agency-specific is unreliable
URL_VEHICLES = "http://gtfs.ovapi.nl/nl/vehiclePositions.pb"
URL_TRIP_UPDATES = "http://gtfs.ovapi.nl/nl/tripUpdates.pb"

# Rotterdam Area Bounding Box (Safety filter)
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
    Parses vehicle positions from the General NL feed.
    """
    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(content)
    except Exception as e:
        print(f"Protobuf Parse Error: {e}")
        return {}, []

    updates = []
    new_state = {}
    current_time = time.time()

    for entity in feed.entity:
        if not entity.HasField('vehicle'): continue
        
        v = entity.vehicle
        pos = v.position
        v_id = str(entity.id)
        
        # 1. Bounding Box Filter (Critical for NL feed)
        if not pos.latitude or not pos.longitude: continue
        if not (bbox_min_lat <= pos.latitude <= bbox_max_lat and bbox_min_lon <= pos.longitude <= bbox_max_lon):
            continue

        # 2. Type Inference Helper
        route_id = v.trip.route_id.upper()
        label = v.vehicle.label if v.vehicle.label else v_id.split(':')[-1]
        v_type = "bus" # Default

        try:
            # Try to infer type from label (vehicle number)
            # RET specific logic:
            if label.isdigit():
                num = int(label)
                if 5000 <= num <= 5800: v_type = "metro"
                elif 2000 <= num <= 2200: v_type = "tram"
            
            # Fallback to route_id analysis
            if v_type == "bus":
                if "METRO" in route_id or route_id.startswith("M-"): v_type = "metro"
                elif "TRAM" in route_id: v_type = "tram"
        except:
            pass

        # --- DESTINATION HUNTING ---
        headsign = trip_headsigns.get(v.trip.trip_id, "")
        
        # Fallback to feed headsign
        if not headsign and v.trip.HasField('trip_headsign'):
            headsign = v.trip.trip_headsign
            
        if not headsign:
            headsign = "Unknown"

        # Speed Calc
        speed = 0.0
        if v_id in old_vehicles:
            prev = old_vehicles[v_id]
            if headsign == "Unknown" and prev["headsign"] != "Unknown":
                headsign = prev["headsign"]
            
            ts_diff = v.timestamp - prev["timestamp"]
            if ts_diff > 0:
                dist = haversine_distance(prev["lat"], prev["lon"], pos.latitude, pos.longitude)
                speed = dist / ts_diff
            else:
                speed = prev["speed"]
        
        if pos.speed > 0: speed = pos.speed * 3.6 # Convert m/s to km/h if provided
        if speed > 120.0: speed = 0.0 # Sanity check

        # Clean Label
        label = v.vehicle.label if v.vehicle.label else v_id.split(':')[-1]
        
        data = {
            "id": v_id,
            "label": label,
            "lat": round(pos.latitude, 6),
            "lon": round(pos.longitude, 6),
            "bearing": round(pos.bearing, 1),
            "speed": round(speed, 1),
            "trip_id": v.trip.trip_id,
            "route_id": v.trip.route_id,
            "headsign": headsign,
            "type": v_type,
            "timestamp": int(v.timestamp if v.timestamp else current_time)
        }

        # Change Detection
        if v_id in old_vehicles:
            old = old_vehicles[v_id]
            if (abs(old["lat"] - data["lat"]) > 0.000001 or abs(old["lon"] - data["lon"]) > 0.000001):
                updates.append(data)
        else:
            updates.append(data)

        new_state[v_id] = data

    return new_state, updates

def parse_stops(content, current_time):
    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(content)
    except:
        return {}, {}, []
    
    new_stops = {}
    new_headsigns = {}
    stop_updates_list = []

    for entity in feed.entity:
        if not entity.HasField('trip_update'): continue
        tu = entity.trip_update
        
        trip_id = tu.trip.trip_id
        
        found_headsign = None
        if tu.trip.HasField('trip_headsign'):
            found_headsign = tu.trip.trip_headsign
        
        if not found_headsign and tu.stop_time_update:
            last_stop = tu.stop_time_update[-1]
            if last_stop.HasField('stop_headsign'):
                found_headsign = last_stop.stop_headsign
        
        if found_headsign:
            new_headsigns[trip_id] = found_headsign

    return new_stops, new_headsigns, stop_updates_list

# --- ASYNC WORKERS ---

async def vehicle_worker():
    global current_vehicles, trip_headsigns
    
    # Updated limits and timeouts
    limits = httpx.Limits(max_keepalive_connections=5)
    async with httpx.AsyncClient(verify=False, limits=limits) as client:
        while True:
            start_time = time.time()
            try:
                # USING SPECIFIC RET FEED
                resp = await client.get(URL_VEHICLES, timeout=5.0)
                if resp.status_code == 200:
                    loop = asyncio.get_running_loop()
                    new_vehicles, updates = await loop.run_in_executor(
                        process_pool, parse_vehicles, resp.content, current_vehicles, trip_headsigns
                    )
                    
                    if len(new_vehicles) > 0:
                         print(f"Active Vehicles: {len(new_vehicles)}", end='\r')
                    
                    current_vehicles = new_vehicles
                    
                    if updates and vehicle_subscribers:
                        msg = json.dumps({"type": "vehicles", "data": updates})
                        for q in list(vehicle_subscribers):
                            if not q.full(): q.put_nowait(msg)
                else:
                    print(f"Fetch Status: {resp.status_code}")

            except Exception as e:
                print(f"Vehicle fetch error: {e}")

            elapsed = time.time() - start_time
            await asyncio.sleep(max(0.5, FETCH_INTERVAL_VEHICLES - elapsed))

async def stop_worker():
    global current_stops, trip_headsigns
    
    limits = httpx.Limits(max_keepalive_connections=5)
    async with httpx.AsyncClient(verify=False, limits=limits, headers={"User-Agent": "RETTracker/2.0"}) as client:
        while True:
            try:
                # USING SPECIFIC RET FEED
                resp = await client.get(URL_TRIP_UPDATES, timeout=10.0)
                if resp.status_code == 200:
                    loop = asyncio.get_running_loop()
                    new_stops, new_headsigns, updates = await loop.run_in_executor(
                        process_pool, parse_stops, resp.content, time.time()
                    )
                    trip_headsigns.update(new_headsigns)

            except Exception as e:
                pass # Silent fail for stops to keep log clean

            await asyncio.sleep(FETCH_INTERVAL_STOPS)

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def health_check():
    return {"status": "ok", "message": "RET Tracker Backend"}

@app.get("/vehicles-sse")
async def vehicles_sse(request: Request):
    async def event_generator():
        q = asyncio.Queue(maxsize=100)
        vehicle_subscribers.add(q)
        try:
            # IMMEDIATE SNAPSHOT
            if current_vehicles:
                yield f"data: {json.dumps({'type': 'vehicles', 'data': list(current_vehicles.values())})}\n\n"
            else:
                # Wait briefly if empty (startup race condition)
                await asyncio.sleep(0.5)
                if current_vehicles:
                    yield f"data: {json.dumps({'type': 'vehicles', 'data': list(current_vehicles.values())})}\n\n"
                    
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
