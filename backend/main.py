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
# EXACT URLs that work
URL_VEHICLES = "http://gtfs.ovapi.nl/nl/vehiclePositions.pb" 
URL_TRIP_UPDATES = "http://gtfs.ovapi.nl/nl/tripUpdates.pb"

# EXACT Bounding Box that works
AREA_MIN_LAT = 51.70  
AREA_MAX_LAT = 52.15  
AREA_MIN_LON = 3.90   
AREA_MAX_LON = 4.85   

# Global State
current_vehicles = {} 
trip_headsigns = {} # Re-enabled headsigns
process_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)

def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2.0)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(delta_lambda/2.0)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def parse_vehicles(content, old_vehicles, trip_headsigns):
    """
    Enhanced Diagnostic Parser.
    Keeps the "Open Gates" logic but adds smart labeling.
    """
    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(content)
    except Exception as e:
        print(f"❌ Parse Error: {e}")
        return {}

    new_state = {}
    current_time = time.time()
    
    for entity in feed.entity:
        if not entity.HasField('vehicle'): continue
        
        v = entity.vehicle
        pos = v.position
        
        # 1. Geographic Filter ONLY (Keep this strict!)
        if not pos.latitude or not pos.longitude: continue
        if abs(pos.latitude) < 1.0: continue
        if not (AREA_MIN_LAT <= pos.latitude <= AREA_MAX_LAT and 
                AREA_MIN_LON <= pos.longitude <= AREA_MAX_LON):
            continue
            
        v_id_str = str(entity.id)
        
        # 2. Smart ID Parsing (The Upgrade)
        # We transform "2026-01-29:RET:15:159272" -> Label "15", Type "Tram"
        parts = v_id_str.split(':')
        
        v_type = "bus"      # Default
        label = "Unknown"   # Default
        is_ret = False

        # Check if it is RET based on ID string
        if "RET" in parts:
            is_ret = True
            try:
                # The label is usually the item right after "RET"
                idx = parts.index("RET")
                if idx + 1 < len(parts):
                    raw = parts[idx+1].upper().strip() # "15", "M009"
                    label = raw
                    
                    # Type Logic
                    if raw.startswith("M") or raw in ["A", "B", "C", "D", "E"]:
                        v_type = "metro"
                    elif raw.isdigit() and int(raw) < 30:
                        v_type = "tram"
                    else:
                        v_type = "bus"
            except:
                pass
        else:
            # If it's QBUZZ/EBS/HTM, just mark it External so frontend colors it Gray
            label = v.trip.route_id # Fallback
        
        # 3. Headsign Logic
        headsign = trip_headsigns.get(v.trip.trip_id, "")
        if not headsign and v.trip.HasField('trip_headsign'):
            headsign = v.trip.trip_headsign
        if not headsign:
            headsign = "Unknown"

        # 4. Speed Logic (Smoother movement)
        speed = 0.0
        bearing = pos.bearing
        
        if v_id_str in old_vehicles:
            prev = old_vehicles[v_id_str]
            # Persist headsign if we lose it
            if headsign == "Unknown" and prev["headsign"] != "Unknown":
                headsign = prev["headsign"]
            
            ts_diff = int(current_time) - prev["timestamp"] # Use local time diff
            if ts_diff > 0:
                dist = haversine_distance(prev["lat"], prev["lon"], pos.latitude, pos.longitude)
                speed = dist / ts_diff
            else:
                speed = prev["speed"]
        
        if pos.speed > 0: speed = pos.speed * 3.6 # Convert m/s to km/h

        # 5. Build Final Data
        data = {
            "id": v_id_str,
            "label": label,          # "15", "E", "33"
            "lat": round(pos.latitude, 6),
            "lon": round(pos.longitude, 6),
            "bearing": round(bearing, 1),
            "speed": round(speed, 1),
            "headsign": headsign,    # "Centraal Station"
            "type": v_type,          # "tram", "metro", "bus"
            "agency": "RET" if is_ret else "External",
            "timestamp": int(current_time)
        }
        
        new_state[v_id_str] = data

    return new_state

def parse_stops(content):
    """Fetches headsigns for the popup."""
    try:
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(content)
        new_headsigns = {}
        for entity in feed.entity:
            if not entity.HasField('trip_update'): continue
            tu = entity.trip_update
            
            hs = None
            if tu.trip.HasField('trip_headsign'): hs = tu.trip.trip_headsign
            elif tu.stop_time_update:
                last = tu.stop_time_update[-1]
                if last.HasField('stop_headsign'): hs = last.stop_headsign
            
            if hs: new_headsigns[tu.trip.trip_id] = hs
        return new_headsigns
    except:
        return {}

# --- WORKERS ---

async def vehicle_worker():
    global current_vehicles, trip_headsigns
    limits = httpx.Limits(max_keepalive_connections=5)
    async with httpx.AsyncClient(verify=False, limits=limits) as client:
        while True:
            try:
                # Using the URL that WORKED
                resp = await client.get(URL_VEHICLES, timeout=10.0)
                if resp.status_code == 200:
                    loop = asyncio.get_running_loop()
                    # Parse in background
                    new_vehicles = await loop.run_in_executor(
                        process_pool, parse_vehicles, resp.content, current_vehicles, trip_headsigns
                    )
                    current_vehicles = new_vehicles
                    print(f"✅ Vehicles Updated: {len(current_vehicles)} found")
                else:
                    print(f"❌ HTTP Error: {resp.status_code}")
            except Exception as e:
                print(f"❌ Worker Error: {e}")
            
            await asyncio.sleep(1.0)

async def stop_worker():
    global trip_headsigns
    async with httpx.AsyncClient(verify=False) as client:
        while True:
            try:
                resp = await client.get(URL_TRIP_UPDATES, timeout=20.0)
                if resp.status_code == 200:
                    loop = asyncio.get_running_loop()
                    new_heads = await loop.run_in_executor(process_pool, parse_stops, resp.content)
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
        while True:
            if await request.is_disconnected(): break
            # Stream the full list every 1s (Proven to work)
            data = list(current_vehicles.values())
            if data:
                yield f"data: {json.dumps({'type': 'vehicles', 'data': data})}\n\n"
            else:
                yield ": keep-alive\n\n"
            
            await asyncio.sleep(1.0)
            
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache", 
            "Connection": "keep-alive", 
            "X-Accel-Buffering": "no"
        },
    )