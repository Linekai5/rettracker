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
# 1. Use the URL that we PROVED works
URL_VEHICLES = "http://gtfs.ovapi.nl/nl/vehiclePositions.pb" 

# 2. Use the Area Box that we PROVED works
AREA_MIN_LAT = 51.70  
AREA_MAX_LAT = 52.15  
AREA_MIN_LON = 3.90   
AREA_MAX_LON = 4.85   

current_vehicles = {} 
process_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)

def parse_vehicles(content):
    """
    The Robust Parser. 
    Accepts any vehicle in the box. No strict filtering.
    """
    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(content)
    except Exception as e:
        print(f"❌ CRITICAL: Failed to parse Protobuf: {e}")
        return {}

    new_state = {}
    current_time = time.time()
    
    for entity in feed.entity:
        if not entity.HasField('vehicle'): continue
        
        v = entity.vehicle
        pos = v.position
        
        # 1. Geographic Filter ONLY (This is the Safety Net)
        if not pos.latitude or not pos.longitude: continue
        if abs(pos.latitude) < 1.0: continue
        
        if (AREA_MIN_LAT <= pos.latitude <= AREA_MAX_LAT and 
            AREA_MIN_LON <= pos.longitude <= AREA_MAX_LON):
            
            v_id = str(entity.id)
            parts = v_id.split(':')
            
            # --- SIMPLE TYPE DETECTION (Safe) ---
            # Default to bus
            v_type = "bus"
            label = v.trip.route_id
            
            # If ID contains RET, try to grab the line number
            if "RET" in parts:
                try:
                    # ID format: DATE:RET:LINE:VEHICLE
                    idx = parts.index("RET")
                    if idx + 1 < len(parts):
                        raw = parts[idx+1]
                        label = raw # "15", "M009"
                        
                        # Logic: Metros start with M, Trams are < 30
                        if raw.startswith("M") or raw in ["A","B","C","D","E"]:
                            v_type = "metro"
                        elif raw.isdigit() and int(raw) < 30:
                            v_type = "tram"
                except:
                    pass

            # Extract basic info
            data = {
                "id": v_id,
                "label": label,
                "lat": round(pos.latitude, 6),
                "lon": round(pos.longitude, 6),
                "heading": pos.bearing,
                "type": v_type, 
                "timestamp": int(current_time)
            }
            
            new_state[v_id] = data

    return new_state

# --- WORKER ---
async def vehicle_worker():
    global current_vehicles
    # 10s Timeout + Loop Protection
    limits = httpx.Limits(max_keepalive_connections=5)
    async with httpx.AsyncClient(verify=False, limits=limits) as client:
        while True:
            try:
                # print("⬇️ Fetching data...", flush=True) 
                resp = await client.get(URL_VEHICLES, timeout=10.0)
                if resp.status_code == 200:
                    loop = asyncio.get_running_loop()
                    new_vehicles = await loop.run_in_executor(process_pool, parse_vehicles, resp.content)
                    current_vehicles = new_vehicles
                    # Only print once every few seconds to confirm it's alive
                    if len(new_vehicles) > 0:
                         print(f"✅ Active: {len(new_vehicles)} vehicles tracked.", end='\r')
                else:
                    print(f"❌ Fetch failed: Status {resp.status_code}")
            except Exception as e:
                print(f"❌ Error in worker: {e}")
            
            await asyncio.sleep(1.0)

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
            # ALWAYS send data if we have it
            data = list(current_vehicles.values())
            if data:
                yield f"data: {json.dumps({'type': 'vehicles', 'data': data})}\n\n"
            else:
                 yield ": keep-alive\n\n"
            await asyncio.sleep(1.0)
            
    return StreamingResponse(event_generator(), media_type="text/event-stream")