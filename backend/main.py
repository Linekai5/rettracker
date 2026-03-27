from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import httpx
import asyncio
import json
import time
from typing import Dict, Set
from cachetools import TTLCache
import hashlib
from collections import defaultdict
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============== PERFORMANCE OPTIMIZATIONS ==============

# Global persistent HTTP client with connection pooling
http_client: httpx.AsyncClient = None

# Cache for API responses (TTL: 2 seconds to handle race conditions)
response_cache = TTLCache(maxsize=100, ttl=2)

# Current vehicle state
current_vehicles: Dict[str, dict] = {}
vehicle_hashes: Dict[str, str] = {}  # Track changes via hash comparison
last_fetch_time = 0

# Current stops state
current_stops: Dict[str, dict] = {}
stop_hashes: Dict[str, str] = {}
last_stops_fetch_time = 0

# Connected SSE clients
active_vehicle_clients: Set[asyncio.Queue] = set()
active_stops_clients: Set[asyncio.Queue] = set()

# Configuration
FETCH_INTERVAL = 2.5       # Faster polling for real-time feel
BATCH_SIZE = 50            # Larger batches = fewer requests
ALLOWED_TYPES = {"TRAM", "METRO", "BUS"}
CONCURRENT_BATCHES = 3     # Parallel batch fetching

# Standard deviations for boundaries (approximate bounding box for the RET network region)
# This box roughly covers Rotterdam and surrounding areas serviced by RET
MIN_LAT = 51.5  # Southern limit
MAX_LAT = 52.2  # Northern limit
MIN_LON = 3.8   # Western limit
MAX_LON = 4.9   # Eastern limit

# ============== HELPERS ==============

def is_valid_coordinate(lat, lon):
    """Check if coordinates are within the valid region"""
    if lat is None or lon is None:
        return False
    # Only allow valid float coordinates
    try:
        lat = float(lat)
        lon = float(lon)
    except (ValueError, TypeError):
        return False
        
    return MIN_LAT <= lat <= MAX_LAT and MIN_LON <= lon <= MAX_LON

def hash_vehicle(v: dict) -> str:
    """Fast hash for detecting changes"""
    key_fields = f"{v['lat']:.6f},{v['lon']:.6f},{v['bearing']},{v['speed']}"
    return hashlib.md5(key_fields.encode()).hexdigest()[:12]

def hash_stop(s: dict) -> str:
    """Fast hash for detecting stops changes (passages)"""
    passages_str = ",".join([f"{p['line']}-{p['expected_arrival']}" for p in s.get('passages', [])])
    return hashlib.md5(passages_str.encode()).hexdigest()[:12]

def normalize_vehicle_data(stop: dict, journey_id: str, stop_id: str) -> dict:
    """Transform API data to frontend-compatible format"""
    transport_type = stop.get("TransportType", "BUS")
    
    # Frontend expects lowercase type
    vehicle_type = transport_type.lower()
    
    # Calculate proper bearing (0-360 degrees)
    bearing = stop.get("Bearing", 0) or 0
    
    # Speed conversion: API gives km/h, keep as-is
    speed = stop.get("Speed", 0) or 0
    
    return {
        "id": f"{journey_id}", # Changed from f"{journey_id}_{stop_id}" to properly track the moving vehicle
        "entity_id": journey_id,  # Alternative ID field for compatibility
        "lat": stop.get("Latitude"),
        "lon": stop.get("Longitude"),
        "line": stop.get("LinePublicNumber", "?"),
        "route_id": stop.get("LinePublicNumber", "?"),  # Duplicate for compatibility
        "bearing": bearing,
        "speed": speed,
        "type": vehicle_type,  # Lowercase: tram, metro, bus
        "destination": stop.get("DestinationName50", "Unknown"),
        "headsign": stop.get("DestinationName50", "Unknown"),  # GTFS-RT compatible
        "delay": stop.get("DelayInSeconds", 0) or 0,
        "timestamp": stop.get("LastUpdateTimeStamp"),
    }

async def fetch_batch(client: httpx.AsyncClient, batch: list) -> dict:
    """Fetch a single batch with caching"""
    url = f"https://v0.ovapi.nl/journey/{','.join(batch)}"
    
    # Check cache
    cache_key = url
    if cache_key in response_cache:
        return response_cache[cache_key]
    
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
        response_cache[cache_key] = data
        return data
    except Exception as e:
        print(f"Batch fetch failed: {e}")
        return {}

async def poll_vehicles_and_stops():
    """Background task: Poll API and update both vehicle and stops global state"""
    global current_vehicles, vehicle_hashes, last_fetch_time, http_client
    global current_stops, stop_hashes, last_stops_fetch_time
    
    # Initialize persistent HTTP client with connection pooling
    http_client = httpx.AsyncClient(
        verify=False,
        timeout=20.0,
        follow_redirects=True,
        limits=httpx.Limits(max_keepalive_connections=20, max_connections=50)
    )
    
    while True:
        try:
            now = time.time()
            
            if now - last_fetch_time < FETCH_INTERVAL:
                await asyncio.sleep(0.5)
                continue
            
            # Fetch journey keys
            resp_keys = await http_client.get("https://v0.ovapi.nl/journey/")
            resp_keys.raise_for_status()
            journey_keys = [k for k in resp_keys.json().keys() if k.startswith("RET_")]
            
            if not journey_keys:
                await asyncio.sleep(2.0)
                continue
            
            # Parallel batch fetching
            new_vehicles = {}
            new_stops = {}
            
            batches = [journey_keys[i:i + BATCH_SIZE] for i in range(0, len(journey_keys), BATCH_SIZE)]
            
            # Process batches concurrently
            for batch_group in [batches[i:i + CONCURRENT_BATCHES] for i in range(0, len(batches), CONCURRENT_BATCHES)]:
                results = await asyncio.gather(*[fetch_batch(http_client, b) for b in batch_group])
                
                for journeys in results:
                    for journey_id, journey in journeys.items():
                        stops = journey.get("Stops", {})
                        for stop_id, stop in stops.items():
                            transport_type = stop.get("TransportType")
                            
                            if transport_type not in ALLOWED_TYPES:
                                continue
                            
                            # Build stop schedules
                            timing_point = stop.get("TimingPointCode")
                            if timing_point and stop.get("TripStopStatus") in ("PLANNED", "DRIVING"):
                                lat = stop.get("Latitude")
                                lon = stop.get("Longitude")
                                
                                # Validate coordinates before adding stop
                                if not is_valid_coordinate(lat, lon):
                                    continue

                                if timing_point not in new_stops:
                                    new_stops[timing_point] = {
                                        "id": timing_point,
                                        "name": stop.get("TimingPointName", "Unknown"),
                                        "lat": lat,
                                        "lon": lon,
                                        "type": transport_type.lower(),
                                        "passages": []
                                    }
                                
                                new_stops[timing_point]["passages"].append({
                                    "journey_id": journey_id,
                                    "line": stop.get("LinePublicNumber", "?"),
                                    "destination": stop.get("DestinationName50", "Unknown"),
                                    "expected_arrival": stop.get("ExpectedArrivalTime", stop.get("TargetArrivalTime")),
                                    "status": stop.get("TripStopStatus"),
                                    "type": transport_type.lower()
                                })

                            if stop.get("TripStopStatus") in ("DRIVING", "ARRIVED", "DEPARTING"):
                                vehicle = normalize_vehicle_data(stop, journey_id, stop_id)
                                
                                # Skip if coordinates missing or invalid
                                if not vehicle["lat"] or not vehicle["lon"] or not is_valid_coordinate(vehicle["lat"], vehicle["lon"]):
                                    continue
                                
                                new_vehicles[vehicle["id"]] = vehicle
            
            # Sort passages for each stop by arrival time and trim to next 5
            for stop_id, stop_data in new_stops.items():
                stop_data["passages"].sort(key=lambda x: x["expected_arrival"] if x["expected_arrival"] else "9999-99-99")
                stop_data["passages"] = stop_data["passages"][:5]

            # Detect vehicle changes via hashing
            changed_vehicles = []
            for vid, vehicle in new_vehicles.items():
                new_hash = hash_vehicle(vehicle)
                if vid not in vehicle_hashes or vehicle_hashes[vid] != new_hash:
                    changed_vehicles.append(vehicle)
                    vehicle_hashes[vid] = new_hash
            
            # Detect stop changes via hashing
            changed_stops = []
            for sid, stop_info in new_stops.items():
                new_hash = hash_stop(stop_info)
                if sid not in stop_hashes or stop_hashes[sid] != new_hash:
                    changed_stops.append(stop_info)
                    stop_hashes[sid] = new_hash

            # Update global state
            current_vehicles = new_vehicles
            current_stops = new_stops
            last_fetch_time = now
            last_stops_fetch_time = now
            
            # Broadcast to vehicle clients
            if changed_vehicles:
                message = json.dumps({"updates": changed_vehicles})
                dead_clients = set()
                for client_queue in active_vehicle_clients:
                    try:
                        client_queue.put_nowait(message)
                    except asyncio.QueueFull:
                        dead_clients.add(client_queue)
                active_vehicle_clients.difference_update(dead_clients)
            
            # Broadcast to stop clients
            if changed_stops:
                message = json.dumps({"updates": changed_stops})
                dead_clients = set()
                for client_queue in active_stops_clients:
                    try:
                        client_queue.put_nowait(message)
                    except asyncio.QueueFull:
                        dead_clients.add(client_queue)
                active_stops_clients.difference_update(dead_clients)
                
            if changed_vehicles or changed_stops:
                print(f"✓ Vehicles: {len(changed_vehicles)} updates ({len(active_vehicle_clients)} clients) | Stops: {len(changed_stops)} updates ({len(active_stops_clients)} clients)")
        
        except Exception as e:
            print(f"Poll error: {e}")
            await asyncio.sleep(2.0)

async def client_stream(client_queue: asyncio.Queue, get_initial_state_func):
    """Stream updates to a single SSE client"""
    try:
        # Send initial snapshot
        initial_state = get_initial_state_func()
        if initial_state:
            initial = json.dumps({"updates": list(initial_state.values())})
            yield f"data: {initial}\n\n"
        
        # Stream updates
        while True:
            message = await client_queue.get()
            yield f"data: {message}\n\n"
    except asyncio.CancelledError:
        pass

def get_initial_vehicles():
    return current_vehicles

def get_initial_stops():
    return current_stops

@app.on_event("startup")
async def startup():
    """Start background polling task"""
    asyncio.create_task(poll_vehicles_and_stops())

@app.on_event("shutdown")
async def shutdown():
    """Cleanup on shutdown"""
    global http_client
    if http_client:
        await http_client.aclose()

@app.get("/vehicles-sse")
async def vehicles_sse(request: Request):
    """SSE endpoint with optimized streaming"""
    client_queue = asyncio.Queue(maxsize=100)
    active_vehicle_clients.add(client_queue)
    
    async def event_generator():
        try:
            async for message in client_stream(client_queue, get_initial_vehicles):
                if await request.is_disconnected():
                    break
                yield message
        finally:
            active_vehicle_clients.discard(client_queue)
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )

@app.get("/stops-sse")
async def stops_sse(request: Request):
    """SSE endpoint with optimized streaming for stops"""
    client_queue = asyncio.Queue(maxsize=100)
    active_stops_clients.add(client_queue)
    
    async def event_generator():
        try:
            async for message in client_stream(client_queue, get_initial_stops):
                if await request.is_disconnected():
                    break
                yield message
        finally:
            active_stops_clients.discard(client_queue)
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )

@app.get("/vehicles")
async def get_vehicles():
    """REST endpoint for current vehicle snapshot"""
    return {
        "vehicles": list(current_vehicles.values()),
        "count": len(current_vehicles),
        "timestamp": last_fetch_time
    }

@app.get("/stops")
async def get_stops():
    """REST endpoint for current stops snapshot"""
    return {
        "stops": list(current_stops.values()),
        "count": len(current_stops),
        "timestamp": last_stops_fetch_time
    }

@app.get("/vehicles/{vehicle_id}")
async def get_vehicle(vehicle_id: str):
    """REST endpoint for a single vehicle snapshot by id"""
    v = current_vehicles.get(vehicle_id)
    if v is None:
        return {
            "error": "not found",
            "id": vehicle_id
        }
    return v

@app.get("/vehicles/{vehicle_id}/live")
async def get_vehicle_live(vehicle_id: str, request: Request):
    """
    Experimental high-frequency direct fetch for a single journey.
    This bypasses the 2.5s global poll and hits OVAPI directly for the freshest data.
    """
    async def single_vehicle_generator():
        last_hash = ""
        while True:
            if await request.is_disconnected():
                break
                
            try:
                # Direct call to OVAPI for this specific journey
                url = f"https://v0.ovapi.nl/journey/{vehicle_id}"
                resp = await http_client.get(url, timeout=5.0)
                if resp.status_code == 200:
                    data = resp.json()
                    journey = data.get(vehicle_id, {})
                    stops = journey.get("Stops", {})
                    
                    # Find the "active" stop to get current lat/lon
                    # OVAPI journeys provide coordinates per stop; we look for the one the vehicle is currently at/approaching
                    active_stop = None
                    for sid in sorted(stops.keys()):
                        s = stops[sid]
                        if s.get("TripStopStatus") in ("DRIVING", "ARRIVED", "DEPARTING"):
                            active_stop = s
                            # Don't break immediately, we want the *latest* active one
                    
                    if active_stop:
                        v = normalize_vehicle_data(active_stop, vehicle_id, "")
                        new_hash = hash_vehicle(v)
                        
                        if new_hash != last_hash:
                            yield f"data: {json.dumps(v)}\n\n"
                            last_hash = new_hash
                
            except Exception as e:
                print(f"Direct fetch error for {vehicle_id}: {e}")
            
            # Poll every 1 second for the specific vehicle
            await asyncio.sleep(1.0)

    return StreamingResponse(
        single_vehicle_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        },
    )

@app.get("/")
async def root():
    return {
        "message": "RET Tracker - Optimized Real-Time Transit",
        "vehicles": len(current_vehicles),
        "vehicle_clients": len(active_vehicle_clients),
        "stops": len(current_stops),
        "stop_clients": len(active_stops_clients),
        "version": "2.0"
    }
