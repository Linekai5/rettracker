from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from contextlib import asynccontextmanager
from google.transit import gtfs_realtime_pb2
import httpx
import asyncio
import json
import time
import concurrent.futures

# --- Configuration ---
URL_VEHICLES = "http://gtfs.ovapi.nl/nl/vehiclePositions.pb" 

# RET Area (Rotterdam)
AREA_MIN_LAT = 51.70  
AREA_MAX_LAT = 52.15  
AREA_MIN_LON = 3.90   
AREA_MAX_LON = 4.85   

current_vehicles = {} 
process_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)

def parse_vehicles(content):
    """
    Parses Protobuf and strictly filters for RET vehicles + formats for Svelte.
    """
    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(content)
    except Exception as e:
        print(f"❌ Protobuf Parse Error: {e}")
        return {}

    new_state = {}
    current_time = time.time()
    
    for entity in feed.entity:
        if not entity.HasField('vehicle'): continue
        
        v = entity.vehicle
        pos = v.position
        
        # 1. Safety Check
        if not pos.latitude or not pos.longitude: continue
        
        # 2. Geographic Filter (Rotterdam Box)
        if not (AREA_MIN_LAT <= pos.latitude <= AREA_MAX_LAT and 
                AREA_MIN_LON <= pos.longitude <= AREA_MAX_LON):
            continue

        v_id = str(entity.id)
        route_id = v.trip.route_id if v.trip.HasField('route_id') else ""
        
        # 3. ROBUST RET FILTER
        # Check if "RET" appears in ID or Route, OR if it matches known Metro patterns
        search_str = (v_id + "|" + route_id).upper()
        
        # Note: We rely on the geographic filter + simple matching because OVAPI IDs vary wildly
        # If it's in Rotterdam and looks like public transit, we want to catch it if possible
        # But specifically aiming for RET
        
        is_ret = "RET" in search_str
        
        if not is_ret:
            # Fallback: Capture Metros (M-A to M-E) even if "RET" tag is missing
            if any(line in route_id for line in ["M-A", "M-B", "M-C", "M-D", "M-E"]):
                is_ret = True
            elif "SUBWAY" in search_str or "METRO" in search_str:
                is_ret = True
        
        # If still not found, check numeric ranges (RET specific)
        if not is_ret:
            label = v.vehicle.label
            if label and label.isdigit():
                 num = int(label)
                 # Metro 5000s, Trams 2000s, Buses 1000s, electric buses
                 if (5000 <= num <= 5800) or (2000 <= num <= 2200):
                     is_ret = True

        if not is_ret:
            continue # Skip non-RET vehicles (Arriva, NS, etc.)

        # 4. Type Detection (Metro, Tram, Bus)
        v_type = "bus" # Default
        if "METRO" in search_str or "SUB" in search_str or any(x in route_id for x in ["M-A", "M-B", "M-C", "M-D", "M-E"]):
            v_type = "metro"
            # Specific label check to confirm
            label = v.vehicle.label
            if label and label.isdigit() and int(label) > 5000: v_type = "metro"
            
        elif "TRAM" in search_str or (route_id.isdigit() and int(route_id) < 30):
             v_type = "tram"
             label = v.vehicle.label
             if label and label.isdigit() and 2000 <= int(label) <= 2200: v_type = "tram"

        # 5. Format for Svelte Frontend
        # specific keys: bearing, speed (km/h), route_id
        data = {
            "id": v_id,
            "lat": round(pos.latitude, 6),
            "lon": round(pos.longitude, 6),
            "bearing": pos.bearing,                      # Correct key for Svelte
            "speed": round((pos.speed or 0) * 3.6, 1),   # Convert m/s to km/h
            "route_id": route_id,                        # Correct key for line badge
            "headsign": route_id,                        # Fallback: PB feed usually lacks headsign
            "type": v_type, 
            "timestamp": int(current_time)
        }
            
        new_state[v_id] = data

    return new_state

# --- IDLE KEEP-ALIVE ---
# Prevents connection drops if 0 vehicles are found initially
async def keep_alive_sender():
    pass 

# --- WORKER ---
async def vehicle_worker():
    global current_vehicles
    limits = httpx.Limits(max_keepalive_connections=5)
    
    async with httpx.AsyncClient(verify=False, limits=limits) as client:
        while True:
            try:
                resp = await client.get(URL_VEHICLES, timeout=10.0)
                if resp.status_code == 200:
                    loop = asyncio.get_running_loop()
                    # Offload CPU intensive parsing to thread pool
                    new_vehicles = await loop.run_in_executor(process_pool, parse_vehicles, resp.content)
                    current_vehicles = new_vehicles
                    
                    if len(new_vehicles) > 0:
                         print(f"✅ RET Active: {len(new_vehicles)}", end='\r')
                    else:
                         print(f"⚠️ No RET vehicles found (Check filters)", end='\r')
                else:
                    print(f"❌ Fetch failed: {resp.status_code}")
            except Exception as e:
                print(f"❌ Worker Error: {e}")
            
            await asyncio.sleep(1.0) # 1s update rate

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

@app.get("/vehicles-sse")
async def vehicles_sse(request: Request):
    async def event_generator():
        while True:
            if await request.is_disconnected(): break
            
            data = list(current_vehicles.values())
            if data:
                # Send valid vehicle data
                yield f"data: {json.dumps({'type': 'vehicles', 'data': data})}\n\n"
            else:
                # Send keep-alive comment so browser doesn't close connection
                yield ": keep-alive\n\n"
                
            await asyncio.sleep(0.5) # Fast UI updates
            
    return StreamingResponse(event_generator(), media_type="text/event-stream")
