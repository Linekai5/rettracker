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
# We use the main NL feed which is the most reliable
URL_VEHICLES = "http://gtfs.ovapi.nl/nl/vehiclePositions.pb" 
URL_TRIP_UPDATES = "http://gtfs.ovapi.nl/nl/tripUpdates.pb"

# WIDE Rotterdam Area (Safety Net)
# Min/Max Lat/Lon covering Rotterdam + Den Haag
AREA_MIN_LAT = 51.70  
AREA_MAX_LAT = 52.15  
AREA_MIN_LON = 3.90   
AREA_MAX_LON = 4.85   

current_vehicles = {} 
process_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)

def parse_vehicles(content):
    """
    Diagnostic Parser: Accepts ANYTHING in the geographic box.
    """
    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(content)
    except Exception as e:
        print(f"❌ CRITICAL: Failed to parse Protobuf: {e}")
        return {}

    updates = []
    new_state = {}
    current_time = time.time()
    
    # Debug Counters
    total_entities = len(feed.entity)
    inside_box = 0
    
    for entity in feed.entity:
        if not entity.HasField('vehicle'): continue
        
        v = entity.vehicle
        pos = v.position
        
        # 1. Geographic Filter ONLY
        if not pos.latitude or not pos.longitude: continue
        if abs(pos.latitude) < 1.0: continue # Skip 0,0 garbage
        
        if (AREA_MIN_LAT <= pos.latitude <= AREA_MAX_LAT and 
            AREA_MIN_LON <= pos.longitude <= AREA_MAX_LON):
            
            inside_box += 1
            v_id = str(entity.id)
            
            # Extract basic info
            data = {
                "id": v_id,
                "lat": round(pos.latitude, 6),
                "lon": round(pos.longitude, 6),
                "heading": pos.bearing,
                "route": v.trip.route_id,
                "type": "bus", # Default to bus for now
                "timestamp": int(current_time)
            }
            
            # Simple Type Guessing
            rid = v.trip.route_id.upper()
            if "METRO" in rid or "RET:M" in rid: data["type"] = "metro"
            elif "TRAM" in rid or "RET:T" in rid: data["type"] = "tram"
            
            new_state[v_id] = data

    print(f"🔍 DIAGNOSTIC: Downloaded {total_entities} entities. Found {inside_box} inside Rotterdam box.")
    
    # Print the FIRST match to see what the IDs look like
    if inside_box > 0:
        first_key = next(iter(new_state))
        print(f"   Sample Vehicle: {new_state[first_key]}")

    return new_state

# --- WORKER ---
async def vehicle_worker():
    global current_vehicles
    limits = httpx.Limits(max_keepalive_connections=5)
    async with httpx.AsyncClient(verify=False, limits=limits) as client:
        while True:
            try:
                print("⬇️ Fetching data...")
                resp = await client.get(URL_VEHICLES, timeout=10.0)
                if resp.status_code == 200:
                    print(f"✅ Data received: {len(resp.content)} bytes")
                    loop = asyncio.get_running_loop()
                    new_vehicles = await loop.run_in_executor(process_pool, parse_vehicles, resp.content)
                    current_vehicles = new_vehicles
                else:
                    print(f"❌ Fetch failed: Status {resp.status_code}")
            except Exception as e:
                print(f"❌ Error in worker: {e}")
            
            await asyncio.sleep(2.0)

# --- APP ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    t1 = asyncio.create_task(vehicle_worker())
    yield
    t1.cancel()
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
            # Send whatever we have, even if empty
            data = list(current_vehicles.values())
            yield f"data: {json.dumps({'type': 'vehicles', 'data': data})}\n\n"
            await asyncio.sleep(1.0) # Slow update for safety
            
    return StreamingResponse(event_generator(), media_type="text/event-stream")