from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from contextlib import asynccontextmanager
import httpx
import asyncio
import json
import time

# --- Configuration ---
FETCH_INTERVAL = 0.5       
BATCH_SIZE = 50            
ALLOWED_TYPES = {"TRAM", "METRO", "BUS"}

# --- Global State ---
# Stores the latest known state of all vehicles
current_vehicles = {} 
# List of queues for connected clients
subscribers = set()

# Helper for parallel requests
async def fetch_batch(client, url):
    try:
        resp = await client.get(url)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return {}

# --- Background Worker ---
async def data_fetcher():
    """
    Runs in the background. 
    Fetches data ONCE per interval and broadcasts to ALL subscribers.
    """
    global current_vehicles
    print("Background fetcher started.")
    
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=50)
    
    async with httpx.AsyncClient(verify=False, timeout=10.0, follow_redirects=True, limits=limits) as client:
        while True:
            start_time = time.time()
            updates = []
            new_vehicles = {}

            try:
                # 1. Get all journey keys
                resp_keys = await client.get("https://v0.ovapi.nl/journey/")
                if resp_keys.status_code != 200:
                    await asyncio.sleep(1)
                    continue

                journey_keys = [k for k in resp_keys.json().keys() if k.startswith("RET_")]

                if not journey_keys:
                    await asyncio.sleep(1)
                    continue

                # 2. Fetch details in parallel batches
                tasks = []
                for i in range(0, len(journey_keys), BATCH_SIZE):
                    batch = journey_keys[i:i + BATCH_SIZE]
                    url = f"https://v0.ovapi.nl/journey/{','.join(batch)}"
                    tasks.append(fetch_batch(client, url))

                # Fire all requests at once
                results = await asyncio.gather(*tasks)

                # 3. Process results
                for journeys in results:
                    if not journeys:
                        continue

                    for journey_id, journey in journeys.items():
                        stops = journey.get("Stops", {})
                        for stop_id, stop in stops.items():
                            transport_type = stop.get("TransportType")

                            if transport_type not in ALLOWED_TYPES:
                                continue

                            if stop.get("TripStopStatus") in ("DRIVING", "ARRIVED", "DEPARTING"):
                                vehicle = {
                                    "id": f"{journey_id}_{stop_id}",
                                    "lat": stop.get("Latitude"),
                                    "lon": stop.get("Longitude"),
                                    "line": stop.get("LinePublicNumber"),
                                    "bearing": stop.get("SideCode", 0),
                                    "speed": stop.get("Speed", 0),
                                    "type": transport_type,
                                    "destination": stop.get("DestinationName50"),
                                    "last_update": stop.get("LastUpdateTimeStamp"),
                                    "delay": stop.get("DelayInSeconds", 0),
                                    "direction": stop.get("Direction", "?"),
                                }

                                # Add to local map for this tick
                                new_vehicles[vehicle["id"]] = vehicle

                                # Check if changed compared to GLOBAL state
                                if (
                                    vehicle["id"] not in current_vehicles
                                    or current_vehicles[vehicle["id"]] != vehicle
                                ):
                                    updates.append(vehicle)

                # Update global state for next tick
                current_vehicles = new_vehicles

                # 4. Broadcast updates to any connected clients
                if updates and subscribers:
                    message = json.dumps({"updates": updates})
                    # Send to every open tab's queue
                    for q in list(subscribers):
                        await q.put(message)

            except Exception as e:
                print(f"Error in fetcher: {e}")

            # Precise sleep to keep blazing fast rate
            elapsed = time.time() - start_time
            sleep_time = max(0.1, FETCH_INTERVAL - elapsed)
            await asyncio.sleep(sleep_time)


# --- Lifecycle Manager ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Start the background loop
    task = asyncio.create_task(data_fetcher())
    yield
    # Shutdown: Stop the loop
    task.cancel()


app = FastAPI(lifespan=lifespan)

@app.get("/vehicles-sse")
async def vehicles_sse(request: Request):
    """
    Each client gets their own Queue.
    The background worker pushes data into this Queue.
    """
    async def event_generator():
        q = asyncio.Queue()
        subscribers.add(q)
        try:
            # 1. Send IMMEDIATE full snapshot upon connection
            # (So user doesn't wait for next tick to see vehicles)
            initial_data = list(current_vehicles.values())
            if initial_data:
                yield f"data: {json.dumps({'updates': initial_data})}\n\n"

            # 2. Loop forever sending updates from the queue
            while True:
                if await request.is_disconnected():
                    break
                    
                data = await q.get()
                yield f"data: {data}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            # Cleanup when tab closes
            subscribers.discard(q)

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
        "message": "RET Tracker - High Performance Broadcaster Active",
        "active_clients": len(subscribers),
        "tracked_vehicles": len(current_vehicles)
    }