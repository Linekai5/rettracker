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
import zmq
import zmq.asyncio
import gzip

# --- Configuration ---
# Vehicles update often (locations). Reduced to 1.0s for faster feel.
FETCH_INTERVAL_VEHICLES = 1.0  
# Stops update less often (ETAs)
FETCH_INTERVAL_STOPS = 10.0
# Only process entities with IDs containing this string
AGENCY_FILTER = "RET"

# Known Active Tram Lines for Strict Detection
# Updated to match user-verified list + known frequent lines
# Explicitly: 1, 2, 3, 4, 5, 6, 7, 8, 11 (from user image)
# Plus: 20, 21, 23, 24, 25 (Citroen/TramPlus lines)
KNOWN_TRAM_LINES = {
    "1", "2", "3", "4", "5", "6", "7", "8", "9", "11", 
    "20", "21", "23", "24", "25"
}

# Rotterdam Area Bounding Box (Approx)
# Filter out GPS errors (0,0) or wild drifts
bbox_min_lat, bbox_max_lat = 51.5, 52.3
bbox_min_lon, bbox_max_lon = 3.8, 4.9

# --- Global State ---
# Map: vehicle_id -> { id, lat, lon, bearing, speed, trip_id, ... }
current_vehicles = {} 
# Map: stop_id -> { id, deps: [ { route, dest, time, delay, trip_id } ] }
current_stops = {}
# Map: trip_id -> headsign
trip_headsigns = {}

# List of queues for connected clients
vehicle_subscribers = {
    "metro": set(),
    "tram": set(),
    "bus": set()
}
stop_subscribers = set()

async def fetch_gtfs_feed(client, url):
    """Fetches and parses a GTFS-Realtime feed."""
    try:
        resp = await client.get(url)
        if resp.status_code == 200:
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(resp.content)
            return feed
    except Exception as e:
        print(f"Error fetching {url}: {e}")
    return None

def is_ret_entity(entity):
    """Checks if the entity belongs to RET."""
    # Logic: usually the entity ID for RET starts with "date:RET:..."
    # or the trip_update.trip.trip_id might contain it.
    # From inspection: id: "2026-01-19:RET:M007:186099"
    return AGENCY_FILTER in entity.id

def haversine_distance(lat1, lon1, lat2, lon2):
    """
    Calculate the great circle distance between two points 
    on the earth (specified in decimal degrees) in meters.
    """
    R = 6371000  # Radius of Earth in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    
    a = math.sin(delta_phi / 2.0) ** 2 + \
        math.cos(phi1) * math.cos(phi2) * \
        math.sin(delta_lambda / 2.0) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    
    return R * c

async def vehicle_worker():
    """Fetches VehiclePositions via ZeroMQ for ultra-low latency updates."""
    global current_vehicles, trip_headsigns
    
    # Setup ZMQ Subscriber
    ctx = zmq.asyncio.Context()
    sock = ctx.socket(zmq.SUB)
    # Standard NDOV-Loket PubSub endpoint (Best Effort)
    sock.connect("tcp://pubsub.besteffort.ndovloket.nl:7658")
    # Subscribe specifically to VehiclePositions
    sock.setsockopt_string(zmq.SUBSCRIBE, "/GOVI/KV78/VehiclePositions")
    
    print("ZeroMQ Listener Active: tcp://pubsub.besteffort.ndovloket.nl:7658")

    while True:
        try:
            # Receive multipart: [topic, gzip_data]
            multipart = await sock.recv_multipart()
            content = gzip.decompress(multipart[1])
            
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(content)
            
            updates_by_type = {"metro": [], "tram": [], "bus": []}
            found_ret_entities = False
            
            if feed:
                for entity in feed.entity:
                    if not entity.HasField('vehicle'):
                        continue
                    if not is_ret_entity(entity):
                        continue
                    
                    found_ret_entities = True
                    v = entity.vehicle
                    pos = v.position
                    v_id = entity.id 
                    label = v.vehicle.label if v.vehicle.label else v_id.split(':')[-1]
                    
                    lat = pos.latitude
                    lon = pos.longitude
                    
                    # Basic 0,0 check
                    if not lat or not lon or (abs(lat) < 1 and abs(lon) < 1):
                        continue
                    
                    # Rotterdam Bounding Box Filter
                    if not (bbox_min_lat <= lat <= bbox_max_lat and bbox_min_lon <= lon <= bbox_max_lon):
                        continue

                    ts = v.timestamp if v.timestamp else int(time.time())
                    
                    headsign = trip_headsigns.get(v.trip.trip_id, "")
                    if not headsign:
                        try:
                            if v.trip.HasField('trip_headsign'):
                                headsign = v.trip.trip_headsign
                        except:
                            pass
                    
                    v_type = "bus" # Default
                    parts = v_id.split(':')
                    line_hint = ""
                    if len(parts) >= 3:
                        if parts[1] == "RET": line_hint = parts[2]
                        elif parts[0] == "RET": line_hint = parts[1]
                    
                    if line_hint:
                        lh_upper = line_hint.upper()
                        if lh_upper in ["A", "B", "C", "D", "E"] or lh_upper.startswith("M"):
                            v_type = "metro"
                        elif lh_upper in KNOWN_TRAM_LINES:
                            v_type = "tram"
                    
                    # 2. Check Route ID as fallback
                    if v_type == "bus" and v.trip.route_id:
                        rid = v.trip.route_id.upper()
                        if any(m in rid for m in ["METRO", "M006", "M007", "M008", "M009", "M010"]):
                            v_type = "metro"
                        elif "TRAM" in rid:
                            v_type = "tram"
                        elif rid in KNOWN_TRAM_LINES:
                            v_type = "tram"

                    speed = 0.0
                    if v_id in current_vehicles:
                        prev = current_vehicles[v_id]
                        if ts > prev["timestamp"]:
                            dist = haversine_distance(prev["lat"], prev["lon"], lat, lon)
                            speed = dist / (ts - prev["timestamp"])
                        else:
                            speed = prev["speed"]
                    
                    if pos.HasField('speed') and pos.speed > 0:
                        speed = pos.speed
                    
                    if speed > 36.0: speed = 0.0

                    vehicle_data = {
                        "id": v_id,
                        "label": label,
                        "lat": round(lat, 6),
                        "lon": round(lon, 6),
                        "bearing": round(pos.bearing, 1) if pos.HasField('bearing') else 0,
                        "speed": round(speed, 1),
                        "trip_id": v.trip.trip_id,
                        "route_id": v.trip.route_id,
                        "headsign": headsign,
                        "type": v_type,
                        "line_hint": line_hint,
                        "timestamp": ts
                    }
                    
                    # Diff Check
                    changed = True
                    if v_id in current_vehicles:
                        old = current_vehicles[v_id]
                        if (old["lat"] == vehicle_data["lat"] and 
                            old["lon"] == vehicle_data["lon"] and 
                            old["bearing"] == vehicle_data["bearing"]):
                            changed = False
                    
                    # Update Global State
                    current_vehicles[v_id] = vehicle_data

                    if changed:
                        updates_by_type[v_type].append(vehicle_data)

            # Cleanup Stale Vehicles (TTL 60s)
            # This handles cases where vehicles disappear from the feed
            now = time.time()
            stale_keys = [k for k, v in current_vehicles.items() if (now - v['timestamp']) > 60]
            for k in stale_keys:
                del current_vehicles[k]
                
            # Broadcast updates
            for vt, items in updates_by_type.items():
                if items:
                    msg = json.dumps({"type": "vehicles", "vehicle_type": vt, "data": items})
                    for q in list(vehicle_subscribers[vt]):
                        try:
                            q.put_nowait(msg)
                        except asyncio.QueueFull:
                            pass

        except Exception as e:
            print(f"ZMQ Worker Error: {e}")
            await asyncio.sleep(1)

async def stop_worker():
    """Fetches TripUpdates and calculates Stop ETAs."""
    global current_stops, trip_headsigns
    
    limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
    async with httpx.AsyncClient(verify=False, timeout=15.0, limits=limits, headers={"User-Agent": "RETTracker/1.0"}) as client:
        while True:
            start_time = time.time()
            new_stops = {} # map: stop_id -> { id: stop_id, departures: [] }
            stop_updates_list = []

            feed = await fetch_gtfs_feed(client, "http://gtfs.ovapi.nl/new/tripUpdates.pb")
            
            if feed:
                for entity in feed.entity:
                    if not entity.HasField('trip_update'):
                        continue
                    if not is_ret_entity(entity):
                        continue
                    
                    tu = entity.trip_update
                    trip_id = tu.trip.trip_id
                    route_id = tu.trip.route_id
                    
                    # Try to find terminus/headsign in StopTimeUpdates
                    # The last stop headsign is usually the destination
                    if tu.stop_time_update:
                        last_stu = tu.stop_time_update[-1]
                        if last_stu.HasField('stop_headsign'):
                            trip_headsigns[trip_id] = last_stu.stop_headsign
                        elif tu.trip.HasField('trip_headsign'): # some producers use this
                            trip_headsigns[trip_id] = tu.trip.trip_headsign

                    # Iterate through stop updates in this trip
                    for stu in tu.stop_time_update:
                        stop_id = stu.stop_id
                        
                        # We want arrival (or departure if arrival missing)
                        # GTFS-RT times are POSIX timestamps
                        arrival_time = stu.arrival.time if stu.arrival.time else stu.departure.time
                        delay = stu.arrival.delay if stu.arrival.HasField('delay') else 0
                        
                        # Filter out old/passed stops (older than 5 mins ago?)
                        # Assuming server time is synced.
                        now = time.time()
                        if arrival_time < now - 300: # data might be slightly old, keep 5 min buffer
                            continue
                        
                        # Filter out too far future? (e.g. > 60 mins)
                        if arrival_time > now + 3600:
                            continue

                        if stop_id not in new_stops:
                            new_stops[stop_id] = {
                                "id": stop_id,
                                "buses": []
                            }
                        
                        new_stops[stop_id]["buses"].append({
                            "trip_id": trip_id,
                            "route_id": route_id,
                            "time": arrival_time,
                            "delay": delay
                        })

                # Post-process: sort and slice
                for stop_id, data in new_stops.items():
                    # Sort buses by arrival time
                    data["buses"].sort(key=lambda x: x["time"])
                    # Keep only next 5 most relevant
                    data["buses"] = data["buses"][:5]
                    
                    # Diff check
                    if stop_id not in current_stops or current_stops[stop_id] != data:
                        stop_updates_list.append(data)
                
                # Update global
                current_stops = new_stops
                
                # Broadcast
                if stop_updates_list and stop_subscribers:
                    msg = json.dumps({"type": "stops", "data": stop_updates_list})
                    for q in list(stop_subscribers):
                        try:
                            q.put_nowait(msg)
                        except asyncio.QueueFull:
                            pass

            elapsed = time.time() - start_time
            await asyncio.sleep(max(1.0, FETCH_INTERVAL_STOPS - elapsed))

# --- Lifecycle Manager ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    t1 = asyncio.create_task(vehicle_worker())
    t2 = asyncio.create_task(stop_worker())
    yield
    # Shutdown
    t1.cancel()
    t2.cancel()


app = FastAPI(lifespan=lifespan)

# --- CORS Configuration ---
origins = [
    "https://www.rettracker.nl",
    "https://rettracker.nl",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Public API: Allow everyone
    allow_credentials=False, # Disable credentials to allow wildcard origin
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def health_check():
    return {"status": "ok", "message": "Backend is running and CORS is configured open."}


@app.get("/vehicles/{vehicle_id}")
async def get_vehicle(vehicle_id: str):
    if vehicle_id in current_vehicles:
        return current_vehicles[vehicle_id]
    return {"error": "Vehicle not found", "id": vehicle_id}

@app.get("/{v_type}-sse")
async def categorical_sse(request: Request, v_type: str):
    if v_type not in ["metro", "tram", "bus"]:
        return {"error": "Invalid vehicle type"}
    
    async def event_generator():
        q = asyncio.Queue(maxsize=150)
        vehicle_subscribers[v_type].add(q)
        try:
            # 1. Send Initial Snapshot for this specific type ONLY
            # This ensures fast initial load of the checked category
            snapshot = [v for v in current_vehicles.values() if v.get("type") == v_type]
            if snapshot:
                yield f"data: {json.dumps({'type': 'vehicles', 'vehicle_type': v_type, 'data': snapshot})}\n\n"

            while True:
                if await request.is_disconnected(): break
                # Queue will only receive updates for this specific v_type 
                # because the vehicle_worker broadcasts to categorical sets.
                data = await q.get()
                yield f"data: {data}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            vehicle_subscribers[v_type].discard(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

@app.get("/stops-sse")
async def stops_sse(request: Request):
    async def event_generator():
        q = asyncio.Queue(maxsize=50)
        stop_subscribers.add(q)
        try:
            # 1. Send Snapshot (Chunked)
            if current_stops:
                all_stops = list(current_stops.values())
                chunk_size = 50
                for i in range(0, len(all_stops), chunk_size):
                    chunk = all_stops[i:i + chunk_size]
                    yield f"data: {json.dumps({'type': 'stops', 'data': chunk})}\n\n"
                    await asyncio.sleep(0.01)

            while True:
                if await request.is_disconnected(): break
                data = await q.get()
                yield f"data: {data}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            stop_subscribers.discard(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

@app.get("/")
async def root():
    return {
        "status": "GTFS-Realtime Broadcaster Active",
        "vehicles_tracked": len(current_vehicles),
        "stops_with_predictions": len(current_stops),
        "clients": len(vehicle_subscribers) + len(stop_subscribers)
    }
