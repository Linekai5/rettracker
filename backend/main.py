from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import httpx
import asyncio
import json
import time
import random

app = FastAPI()

current_vehicles = {}
last_fetch_time = 0
FETCH_INTERVAL = 0.5       # Set to 0.5s for "blazing fast" updates
BATCH_SIZE = 50            # Increased batch size slightly
ALLOWED_TYPES = {"TRAM", "METRO", "BUS"}  # Alles wat je wilt (pas aan)

# Helper function for parallel requests
async def fetch_batch(client, url):
    try:
        resp = await client.get(url)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return {}

async def vehicle_updates():
    global current_vehicles, last_fetch_time

    # Increased limits to allow parallel connections
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=50)
    
    # verify=False om SSL mismatch te fixen
    async with httpx.AsyncClient(verify=False, timeout=10.0, follow_redirects=True, limits=limits) as client:
        while True:
            updates = []
            now = time.time()

            try:
                resp_keys = await client.get("https://v0.ovapi.nl/journey/")
                resp_keys.raise_for_status()

                journey_keys = [k for k in resp_keys.json().keys() if k.startswith("RET_")]

                if not journey_keys:
                    print("Geen RET journeys")
                    await asyncio.sleep(1.0)
                    continue

                new_vehicles = {}
                # print(f"Haal {len(journey_keys)} RET journeys op...") # Commented out to reduce log spam

                # Prepare all batch requests
                tasks = []
                for i in range(0, len(journey_keys), BATCH_SIZE):
                    batch = journey_keys[i:i + BATCH_SIZE]
                    url = f"https://v0.ovapi.nl/journey/{','.join(batch)}"
                    tasks.append(fetch_batch(client, url))

                # Fire all requests at once (Parallel execution)
                results = await asyncio.gather(*tasks)

                # Process results
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
                                    "type": transport_type,  # TRAM, METRO, BUS
                                    "destination": stop.get("DestinationName50"),
                                    "last_update": stop.get("LastUpdateTimeStamp"),  # Timestamp!
                                    "delay": stop.get("DelayInSeconds", 0),
                                    "direction": stop.get("Direction", "?"),
                                }

                                new_vehicles[vehicle["id"]] = vehicle

                                if (
                                    vehicle["id"] not in current_vehicles
                                    or current_vehicles[vehicle["id"]] != vehicle
                                ):
                                    updates.append(vehicle)

                current_vehicles = new_vehicles
                last_fetch_time = now
                # print(f"Update klaar - {len(updates)} veranderd")

            except Exception as e:
                print(f"Hoofd fetch fout: {str(e)}")

            if updates:
                yield f"data: {json.dumps({'updates': updates})}\n\n"

            # Calculate sleep to maintain ~1s loop if processing was fast, 
            # or run immediately if processing took longer than interval.
            elapsed = time.time() - now
            sleep_time = max(0.1, FETCH_INTERVAL - elapsed)
            await asyncio.sleep(sleep_time)


@app.get("/vehicles-sse")
async def vehicles_sse(request: Request):
    return StreamingResponse(
        vehicle_updates(),
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
        "message": "RET Tram/Metro/Bus Tracker – live elke ~8s"
    }