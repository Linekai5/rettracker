from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import httpx
import asyncio
import json
import time
from typing import Dict, Set
from cachetools import TTLCache
import hashlib

app = FastAPI()

# ============== PERFORMANCE OPTIMIZATIONS ==============

# Global persistent HTTP client with connection pooling
http_client: httpx.AsyncClient = None

# Cache for API responses (TTL: 2 seconds to handle race conditions)
response_cache = TTLCache(maxsize=100, ttl=2)

# Current vehicle state
current_vehicles: Dict[str, dict] = {}
vehicle_hashes: Dict[str, str] = {}  # Track changes via hash comparison
last_fetch_time = 0

# Connected SSE clients
active_clients: Set[asyncio.Queue] = set()

# Configuration
FETCH_INTERVAL = 2.5       # Faster polling for real-time feel
BATCH_SIZE = 50            # Larger batches = fewer requests
ALLOWED_TYPES = {"TRAM", "METRO", "BUS"}
CONCURRENT_BATCHES = 3     # Parallel batch fetching

# ============== HELPERS ==============

def hash_vehicle(v: dict) -> str:
    """Fast hash for detecting changes"""
    key_fields = f"{v['lat']:.6f},{v['lon']:.6f},{v['bearing']},{v['speed']}"
    return hashlib.md5(key_fields.encode()).hexdigest()[:12]

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
        "id": f"{journey_id}_{stop_id}",
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

async def poll_vehicles():
    """Background task: Poll API and update global state"""
    global current_vehicles, vehicle_hashes, last_fetch_time, http_client
    
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
                            
                            if stop.get("TripStopStatus") in ("DRIVING", "ARRIVED", "DEPARTING"):
                                vehicle = normalize_vehicle_data(stop, journey_id, stop_id)
                                
                                # Skip if coordinates missing
                                if not vehicle["lat"] or not vehicle["lon"]:
                                    continue
                                
                                new_vehicles[vehicle["id"]] = vehicle
            
            # Detect changes via hashing
            changed_vehicles = []
            for vid, vehicle in new_vehicles.items():
                new_hash = hash_vehicle(vehicle)
                if vid not in vehicle_hashes or vehicle_hashes[vid] != new_hash:
                    changed_vehicles.append(vehicle)
                    vehicle_hashes[vid] = new_hash
            
            # Update global state
            current_vehicles = new_vehicles
            last_fetch_time = now
            
            # Broadcast to all connected clients
            if changed_vehicles:
                message = json.dumps({"updates": changed_vehicles})
                dead_clients = set()
                
                for client_queue in active_clients:
                    try:
                        client_queue.put_nowait(message)
                    except asyncio.QueueFull:
                        dead_clients.add(client_queue)
                
                # Clean up disconnected clients
                active_clients.difference_update(dead_clients)
                
                print(f"✓ {len(changed_vehicles)} updates → {len(active_clients)} clients")
        
        except Exception as e:
            print(f"Poll error: {e}")
            await asyncio.sleep(2.0)

async def client_stream(client_queue: asyncio.Queue):
    """Stream updates to a single SSE client"""
    try:
        # Send initial snapshot
        if current_vehicles:
            initial = json.dumps({"updates": list(current_vehicles.values())})
            yield f"data: {initial}\n\n"
        
        # Stream updates
        while True:
            message = await client_queue.get()
            yield f"data: {message}\n\n"
    except asyncio.CancelledError:
        pass

@app.on_event("startup")
async def startup():
    """Start background polling task"""
    asyncio.create_task(poll_vehicles())


@app.on_event("startup")
async def startup():
    """Start background polling task"""
    asyncio.create_task(poll_vehicles())

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
    active_clients.add(client_queue)
    
    async def event_generator():
        try:
            async for message in client_stream(client_queue):
                if await request.is_disconnected():
                    break
                yield message
        finally:
            active_clients.discard(client_queue)
    
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

@app.get("/")
async def root():
    return {
        "message": "RET Tracker - Optimized Real-Time Transit",
        "vehicles": len(current_vehicles),
        "clients": len(active_clients),
        "version": "2.0"
    }