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
# Using the NL feed (Proven to work in Diagnostic)
URL_VEHICLES = "http://gtfs.ovapi.nl/nl/vehiclePositions.pb" 
URL_TRIP_UPDATES = "http://gtfs.ovapi.nl/nl/tripUpdates.pb"

# Rotterdam & Den Haag Region
# If a vehicle is in this box, we show it. No questions asked.
AREA_MIN_LAT = 51.70  
AREA_MAX_LAT = 52.15  
AREA_MIN_LON = 3.90   
AREA_MAX_LON = 4.85   

# --- Global State ---
current_vehicles = {} 
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
        print(f"❌ Parse Error: {e}")
        return {}, []

    updates = []
    new_state = {}
    current_time = time.time()
    
    count_total = 0
    
    for entity in feed.entity:
        if not entity.HasField('vehicle'): continue
        
        v = entity.vehicle
        pos = v.position
        
        # 1. Geographic Filter (The ONLY strict filter)
        if not pos.latitude or not pos.longitude: continue
        if abs(pos.latitude) < 1.0: continue
        if not (AREA_MIN_LAT <= pos.latitude <= AREA_MAX_LAT and AREA_MIN_LON <= pos.longitude <= AREA_MAX_LON):
            continue
        
        count_total += 1
        
        # 2. Extract Info (No filtering, just extraction)
        v_id_str = str(entity.id)
        parts = v_id_str.split(':')
        
        # Default State
        v_type = "bus"
        line_label = "Unknown"
        is_ret = "RET" in parts
        
        # Try to parse line number for display
        # ID Example: "2026-01-29:RET:15:159272"
        if is_ret:
            try:
                idx = parts.index("RET")
                if idx + 1 < len(parts):
                    raw_line = parts[idx+1].upper().strip() # "15", "M009"
                    line_label = raw_line
                    
                    # Determine Type (Metro/Tram/Bus)
                    if raw_line.startswith("M") or raw_line in ["A", "B", "C", "D", "E"]:
                        v_type = "metro"
                    elif raw_line.isdigit() and int(raw_line) < 30:
                        v_type = "tram"
            except:
                pass
        
        # 3. Headsign Logic
        headsign = trip_headsigns.get(v.trip.trip_id, "")
        if not headsign and v.trip.HasField('trip_headsign'):
            headsign = v.trip.trip_headsign
        if not headsign:
            headsign = "Unknown"

        # 4. Speed Logic
        speed = 0.0
        if v_id_str in old_vehicles:
            prev = old_vehicles[v_id_str]
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

        data = {
            "id": v_id_str,
            "label": line_label,
            "lat": round(pos.latitude, 6),
            "lon": round(pos.longitude, 6),
            "bearing": round(pos.bearing, 1),
            "speed": round(speed, 1),
            "trip_id": v.trip.trip_id,
            "route_id": v.trip.route_id,
            "headsign": headsign,
            "type": v_type, # metro, tram, bus
            "agency": "RET" if is_ret else "External",
            "timestamp": int(v.timestamp if v.timestamp else current_time)
        }

        # Change Detection
        if v_id_str in old_vehicles:
            old = old_vehicles[v_id_str]
            if (abs(old["lat"] - data["lat"]) > 0.000001 or abs(old["lon"] - data["lon"]) > 0.000001):
                updates.append(data)
        else:
            updates.append(data)

        new_state[v_id_str] = data

    print(f"✅ Vehicles in Zone: {count_total} (Streaming to Frontend)", flush=True)
    return new_state, updates

def parse_stops(content, current_time):
    try:
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(content)
    except:
        return {}, {}, []
    
    new_headsigns = {}
    for entity in feed.entity:
        if not entity.HasField('trip_update'): continue
        tu = entity.trip_update
        
        # Capture headsigns for everything
        hs = None
        if tu.trip.HasField('trip_headsign'): hs = tu.trip.trip_headsign
        elif tu.stop_time_update:
            last = tu.stop_time_update[-1]
            if last.HasField('stop_headsign'): hs = last.stop_headsign
            
        if hs: new_headsigns[tu.trip.trip_id] = hs
    return {}, new_headsigns, []

# --- WORKERS ---

async def vehicle_worker():
    global current_vehicles, trip_headsigns
    # HIGH TIMEOUT (30s) prevents "0 vehicles" error on slow download
    limits = httpx.Limits(max_keepalive_connections=5)
    async with httpx.AsyncClient(verify=False, limits=limits) as client:
        while True:
            try:
                resp = await client.get(URL_VEHICLES, timeout=30.0)
                if resp.status_code == 200:
                    loop = asyncio.get_running_loop()
                    new_vehicles, updates = await loop.run_in_executor(
                        process_pool, parse_vehicles, resp.content, current_vehicles, trip_headsigns
                    )
                    current_vehicles = new_vehicles
                    
                    if updates and vehicle_subscribers:
                        msg = json.dumps({"type": "vehicles", "data": updates})
                        for q in list(vehicle_subscribers):
                            if not q.full(): q.put_nowait(msg)
                else:
                    print(f"❌ API Error: {resp.status_code}")
            except Exception as e:
                print(f"❌ Worker Error: {e}")
            
            await asyncio.sleep(1.0)

async def stop_worker():
    global trip_headsigns
    async with httpx.AsyncClient(verify=False) as client:
        while True:
            try:
                resp = await client.get(URL_TRIP_UPDATES, timeout=30.0)
                if resp.status_code == 200:
                    loop = asyncio.get_running_loop()
                    _, new_heads, _ = await loop.run_in_executor(
                        process_pool, parse_stops, resp.content, time.time()
                    )
                    trip_headsigns.update(new_heads)
            except:
                pass
            await asyncio.sleep(15.0)

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
            snapshot = list(current_vehicles.values())
            yield f"data: {json.dumps({'type': 'vehicles', 'data': snapshot})}\n\n"

            while True:
                if await request.is_disconnected(): break
                try:
                    data = await asyncio.wait_for(q.get(), timeout=10.0)
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
            "X-Accel-Buffering": "no"
        },
    )