from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from contextlib import asynccontextmanager
from google.transit import gtfs_realtime_pb2
import httpx
import asyncio
import json
import time

# --- Configuration ---
# Vehicles update often (locations)
FETCH_INTERVAL_VEHICLES = 2.0  
# Stops update less often (ETAs)
FETCH_INTERVAL_STOPS = 10.0
# Only process entities with IDs containing this string
AGENCY_FILTER = "RET"

# --- Global State ---
# Map: vehicle_id -> { id, lat, lon, bearing, speed, trip_id, ... }
current_vehicles = {} 
# Map: stop_id -> { id, deps: [ { route, dest, time, delay, trip_id } ] }
current_stops = {}

# List of queues for connected clients
vehicle_subscribers = set()
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

async def vehicle_worker():
    """Fetches VehiclePositions and broadcasts updates."""
    global current_vehicles
    
    limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
    async with httpx.AsyncClient(verify=False, timeout=10.0, limits=limits, headers={"User-Agent": "RETTracker/1.0"}) as client:
        while True:
            start_time = time.time()
            new_vehicles = {}
            updates = []
            
            feed = await fetch_gtfs_feed(client, "http://gtfs.ovapi.nl/new/vehiclePositions.pb")
            
            if feed:
                for entity in feed.entity:
                    if not entity.HasField('vehicle'):
                        continue
                    if not is_ret_entity(entity):
                        continue
                    
                    v = entity.vehicle
                    pos = v.position
                    
                    # Core data required for checking
                    v_id = entity.id 
                    # If label is available, use it as a display number, else fallback
                    label = v.vehicle.label if v.vehicle.label else v_id.split(':')[-1]
                    
                    vehicle_data = {
                        "id": v_id,
                        "label": label,
                        "lat": round(pos.latitude, 6),
                        "lon": round(pos.longitude, 6),
                        "bearing": round(pos.bearing, 1) if pos.HasField('bearing') else 0,
                        "speed": round(pos.speed, 1) if pos.HasField('speed') else 0,
                        "trip_id": v.trip.trip_id,
                        "route_id": v.trip.route_id,
                        "timestamp": v.timestamp
                    }
                    
                    new_vehicles[v_id] = vehicle_data

                    # Diff Check
                    # If vehicle is new OR data has changed significantly
                    if v_id not in current_vehicles:
                        updates.append(vehicle_data)
                    else:
                        old = current_vehicles[v_id]
                        # Only broadcast if moved keys changed
                        if (old["lat"] != vehicle_data["lat"] or 
                            old["lon"] != vehicle_data["lon"] or
                            old["bearing"] != vehicle_data["bearing"]):
                            updates.append(vehicle_data)

                # Update global state
                # We do NOT clear old vehicles immediately to avoid flickering if one frame fails,
                # but for simplicity in this "live" view we usually replace the whole state 
                # or have a timeout mechanism. For now, full replacement of active vehicles.
                current_vehicles = new_vehicles
                
                # Broadcast
                if updates and vehicle_subscribers:
                    msg = json.dumps({"type": "vehicles", "data": updates})
                    for q in list(vehicle_subscribers):
                        try:
                            q.put_nowait(msg)
                        except asyncio.QueueFull:
                            pass

            elapsed = time.time() - start_time
            await asyncio.sleep(max(0.1, FETCH_INTERVAL_VEHICLES - elapsed))

async def stop_worker():
    """Fetches TripUpdates and calculates Stop ETAs."""
    global current_stops
    
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

@app.get("/vehicles-sse")
async def vehicles_sse(request: Request):
    async def event_generator():
        q = asyncio.Queue(maxsize=100)
        vehicle_subscribers.add(q)
        try:
            # 1. Send Snapshot
            if current_vehicles:
                # Convert dict to list for initial payload
                snapshot = list(current_vehicles.values())
                yield f"data: {json.dumps({'type': 'vehicles', 'data': snapshot})}\n\n"

            while True:
                if await request.is_disconnected(): break
                data = await q.get()
                yield f"data: {data}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            vehicle_subscribers.discard(q)

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
            "Access-Control-Allow-Origin": "*",
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
