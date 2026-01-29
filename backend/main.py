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

# ✅ FIX 1: Use the specific RET feed (Faster, less junk data)
URL_VEHICLES = "http://gtfs.ovapi.nl/new/vehiclePositions.pb" 
URL_TRIP_UPDATES = "http://gtfs.ovapi.nl/new/tripUpdates.pb"

# Rotterdam Area Bounding Box (Slightly widened to catch edge cases)
bbox_min_lat, bbox_max_lat = 51.4, 52.4
bbox_min_lon, bbox_max_lon = 3.6, 5.0

# --- Global State ---
current_vehicles = {} 
current_stops = {}
trip_headsigns = {} 

vehicle_subscribers = set()
process_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)

def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2.0)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(delta_lambda/2.0)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

# --- PARSING LOGIC ---

def parse_vehicles(content, old_vehicles, trip_headsigns):
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
        
        v_id = str(entity.id)
        
        # ✅ FIX 2: Relaxed Filtering
        # If the ID doesn't contain RET, we assume it's not ours and skip it.
        # But we check multiple fields to be sure.
        is_ret = "RET" in v_id.upper() or "RET" in entity.vehicle.trip.route_id.upper()
        if not is_ret:
            continue

        v = entity.vehicle
        pos = v.position
        
        # Basic GPS Check
        if not pos.latitude or not pos.longitude: continue
        if abs(pos.latitude) < 1.0: continue

        # Bounding Box (Kept wide)
        if not (bbox_min_lat <= pos.latitude <= bbox_max_lat and bbox_min_lon <= pos.longitude <= bbox_max_lon):
            continue

        # --- Type Detection ---
        route_id = v.trip.route_id.upper()
        v_type = "bus" # Default
        
        # RET Route Logic
        if "METRO" in route_id or route_id.startswith("RET:M"):
            v_type = "metro"
        elif "TRAM" in route_id:
            v_type = "tram"
        # Heuristic: Short route IDs (e.g. "23") are often Trams in Rotterdam
        elif len(route_id) > 4 and route_id[4:].isdigit() and int(route_id[4:]) < 30:
            v_type = "tram"

        # --- Headsign ---
        headsign = trip_headsigns.get(v.trip.trip_id, "")
        if not headsign and v.trip.HasField('trip_headsign'):
            headsign = v.trip.trip_headsign
        if not headsign:
            headsign = "Unknown"

        # Speed Calc
        speed = 0.0
        if v_id in old_vehicles:
            prev = old_vehicles[v_id]
            # Persist Headsign
            if headsign == "Unknown" and prev["headsign"] != "Unknown":
                headsign = prev["headsign"]
            
            ts_diff = v.timestamp - prev["timestamp"]
            if ts_diff > 0:
                dist = haversine_distance(prev["lat"], prev["lon"], pos.latitude, pos.longitude)
                speed = dist / ts_diff
            else:
                speed = prev["speed"]
        
        if pos.speed > 0: speed = pos.speed * 3.6
        if speed > 130.0: speed = 0.0 

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

        # Change Detection (Reduced threshold to ensure updates flow)
        if v_id in old_vehicles:
            old = old_vehicles[v_id]
            if (abs(old["lat"] - data["lat"]) > 0.000001 or abs(old["lon"] - data["lon"]) > 0.000001):
                updates.append(data)
        else:
            updates.append(data)

        new_state[v_id] = data

    return new_state, updates

def parse_stops(content, current_time):
    # (Kept simple to avoid blocking)
    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(content)
    except:
        return {}, {}, []
    
    new_headsigns = {}
    for entity in feed.entity:
        if not entity.HasField('trip_update'): continue
        tu = entity.trip_update
        if "RET" not in tu.trip.trip_id: continue
        
        # Grab headsign from trip update to feed into vehicle list
        hs = None
        if tu.trip.HasField('trip_headsign'): hs = tu.trip.trip_headsign
        elif tu.stop_time_update:
            last = tu.stop_time_update[-1]
            if last.HasField('stop_headsign'): hs = last.stop_headsign
            
        if hs: new_headsigns[tu.trip.trip_id] = hs

    return {}, new_headsigns, []

# --- ASYNC WORKERS ---

async def vehicle_worker():
    global current_vehicles, trip_headsigns
    limits = httpx.Limits(max_keepalive_connections=5)
    async with httpx.AsyncClient(verify=False, limits=limits) as client:
        while True:
            try:
                resp = await client.get(URL_VEHICLES, timeout=5.0)
                if resp.status_code == 200:
                    loop = asyncio.get_running_loop()
                    new_vehicles, updates = await loop.run_in_executor(
                        process_pool, parse_vehicles, resp.content, current_vehicles, trip_headsigns
                    )
                    current_vehicles = new_vehicles
                    
                    # ✅ FIX 3: Force send updates if we have active vehicles, even if small changes
                    if updates and vehicle_subscribers:
                        msg = json.dumps({"type": "vehicles", "data": updates})
                        for q in list(vehicle_subscribers):
                            if not q.full(): q.put_nowait(msg)
                else:
                    print(f"Fetch failed: {resp.status_code}")
            except Exception as e:
                print(f"Error: {e}")
            await asyncio.sleep(FETCH_INTERVAL_VEHICLES)

async def stop_worker():
    global trip_headsigns
    async with httpx.AsyncClient(verify=False) as client:
        while True:
            try:
                resp = await client.get(URL_TRIP_UPDATES, timeout=10.0)
                if resp.status_code == 200:
                    loop = asyncio.get_running_loop()
                    _, new_heads, _ = await loop.run_in_executor(
                        process_pool, parse_stops, resp.content, time.time()
                    )
                    trip_headsigns.update(new_heads)
            except:
                pass
            await asyncio.sleep(FETCH_INTERVAL_STOPS)

# --- APP ---

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
    return {"status": "ok", "vehicle_count": len(current_vehicles)}

@app.get("/vehicles-sse")
async def vehicles_sse(request: Request):
    async def event_generator():
        q = asyncio.Queue(maxsize=100)
        vehicle_subscribers.add(q)
        try:
            # ✅ FIX 4: Send IMMEDIATE snapshot upon connection
            # If current_vehicles is empty, we send an empty list just to confirm connection works
            snapshot = list(current_vehicles.values())
            yield f"data: {json.dumps({'type': 'vehicles', 'data': snapshot})}\n\n"

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
        headers={
            "Cache-Control": "no-cache", 
            "Connection": "keep-alive", 
            "X-Accel-Buffering": "no" # Critical for Nginx/Cloudflare
        },
    )