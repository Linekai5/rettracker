from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from contextlib import asynccontextmanager
import httpx
import asyncio
import json
import time
import concurrent.futures

# --- CONFIGURATION ---
# Switching to the JSON API (v0)
# This returns a dictionary of ALL vehicles in NL.
URL_POSITIONS = "http://v0.ovapi.nl/pos/"

# --- STATE ---
current_vehicles = {} 
last_update_time = 0

process_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)

def parse_json_job(content):
    """
    Parses the OVAPI v0 JSON.
    Format: { "RET_1234_1": { "Lat": 51.9, "Lon": 4.4, "Velocity": 10, ... }, ... }
    """
    try:
        raw_data = json.loads(content)
    except:
        return {}

    new_state = {}
    timestamp = int(time.time())
    
    found_count = 0

    # Iterate over every vehicle in the Netherlands
    for key, v in raw_data.items():
        # --- THE MAGIC FILTER ---
        # The keys usually look like "RET_M009_1" or "RET_25_2"
        # We just check if "RET" is in the key string.
        if "RET" not in key.upper():
            continue
            
        found_count += 1
        
        # Extract Lat/Lon directly
        lat = v.get("Lat")
        lon = v.get("Long") # Note: v0 uses 'Long', not 'Lon'
        
        if not lat or not lon: continue

        # --- SMART LABELING ---
        # Key format is typically AGENCY_LINE_VARIANT (e.g. RET_23_1)
        parts = key.split('_')
        label = "RET"
        v_type = "bus"
        
        # Try to grab the middle part (Line Number)
        if len(parts) >= 2:
            raw_line = parts[1] # "23", "M009"
            label = raw_line
            
            # Metro
            if raw_line.startswith("M") or raw_line in ["A", "B", "C", "D", "E"]:
                v_type = "metro"
            # Tram (Numbers 1-29)
            elif raw_line.isdigit() and int(raw_line) < 30:
                v_type = "tram"
        
        # Destination is often in 'DestinationName'
        headsign = v.get("DestinationName", "Unknown")

        # Create unique ID (Use the key)
        v_id = key

        new_state[v_id] = {
            "id": v_id,
            "label": label,
            "headsign": headsign,
            "type": v_type,
            "lat": float(lat),
            "lon": float(lon),
            "bearing": int(v.get("Bearing", 0)),
            "speed": float(v.get("Velocity", 0)), # v0 gives km/h directly usually
            "timestamp": timestamp
        }

    print(f"✅ Processed v0 JSON. Found {found_count} RET vehicles.", flush=True)
    return new_state

# --- WORKER ---
async def fetch_loop():
    global current_vehicles, last_update_time
    
    # v0 is fast, but let's be polite. 5s refresh is standard for JSON APIs.
    limits = httpx.Limits(max_keepalive_connections=5)
    async with httpx.AsyncClient(verify=False, limits=limits, timeout=30.0) as client:
        while True:
            try:
                # print("⬇️ Fetching v0 JSON...", flush=True)
                resp = await client.get(URL_POSITIONS)
                if resp.status_code == 200:
                    loop = asyncio.get_running_loop()
                    current_vehicles = await loop.run_in_executor(
                        process_pool, parse_json_job, resp.content
                    )
                    last_update_time = time.time()
                else:
                    print(f"❌ API Error: {resp.status_code}", flush=True)
            except Exception as e:
                print(f"❌ Network Error: {e}", flush=True)
            
            # v0 cache is usually ~10s, so pulling every 2s is safe
            await asyncio.sleep(2.0)

# --- APP ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(fetch_loop())
    yield
    task.cancel()
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
def index():
    return {"status": "RET Tracker (v0 API)", "vehicles": len(current_vehicles)}

@app.get("/vehicles-sse")
async def vehicles_sse(request: Request):
    async def event_generator():
        while True:
            if await request.is_disconnected(): break
            
            data = list(current_vehicles.values())
            
            # Send Data + Last Update Timestamp
            msg = {
                "type": "vehicles",
                "last_updated": last_update_time,
                "data": data
            }
            
            if data:
                yield f"data: {json.dumps(msg)}\n\n"
            else:
                yield ": keep-alive\n\n"
                
            await asyncio.sleep(1.0)

    return StreamingResponse(event_generator(), media_type="text/event-stream")