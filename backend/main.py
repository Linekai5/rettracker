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
FETCH_INTERVAL = 8.0       # seconds - realistic production value
BATCH_SIZE = 40            # safe batch size
ALLOWED_TYPES = {"TRAM"}   # only trams

async def vehicle_updates():
    global current_vehicles, last_fetch_time

    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
        while True:
            updates = []
            now = time.time()

            if now - last_fetch_time >= FETCH_INTERVAL:
                try:
                    # 1. Get list of active journeys
                    resp_keys = await client.get("https://v0.ovapi.nl/journey/")
                    resp_keys.raise_for_status()

                    journey_keys = [
                        k for k in resp_keys.json().keys()
                        if k.startswith("RET_")
                    ]

                    if not journey_keys:
                        print("No RET journeys found")
                        await asyncio.sleep(2.0)
                        continue

                    new_vehicles = {}
                    print(f"Fetching {len(journey_keys)} RET journeys...")

                    # 2. Batch fetch
                    for i in range(0, len(journey_keys), BATCH_SIZE):
                        batch = journey_keys[i:i + BATCH_SIZE]
                        url = f"https://v0.ovapi.nl/journey/{','.join(batch)}"

                        try:
                            resp = await client.get(url)
                            resp.raise_for_status()
                            journeys = resp.json()

                            for journey_id, journey in journeys.items():
                                stops = journey.get("Stops", {})
                                for stop_id, stop in stops.items():
                                    transport_type = stop.get("TransportType")

                                    if transport_type not in ALLOWED_TYPES:
                                        continue

                                    if stop.get("TripStopStatus") in ("DRIVING", "ARRIVED"):
                                        vehicle = {
                                            "id": f"{journey_id}_{stop_id}",
                                            "lat": stop.get("latitude"),
                                            "lon": stop.get("longitude"),
                                            "line": stop.get("LinePublicNumber"),
                                            "bearing": stop.get("SideCode"),
                                            "speed": stop.get("Speed", 0),
                                            "type": transport_type,
                                            "destination": stop.get("DestinationName50"),
                                            "last_update": stop.get("LastUpdateTimeStamp"),
                                        }

                                        new_vehicles[vehicle["id"]] = vehicle

                                        if (
                                            vehicle["id"] not in current_vehicles
                                            or current_vehicles[vehicle["id"]] != vehicle
                                        ):
                                            updates.append(vehicle)

                        except Exception as e:
                            print(f"Batch failed: {url} → {str(e)}")
                            # Continue with next batch

                        await asyncio.sleep(0.15)  # small delay between batches

                    # Update global state
                    current_vehicles = new_vehicles
                    last_fetch_time = now
                    print(f"Update complete - {len(updates)} vehicles changed")

                except Exception as e:
                    print(f"Fetch error: {str(e)}")
                    # Don't crash the loop

            # Send updates only if we have something
            if updates:
                yield f"data: {json.dumps({'updates': updates})}\n\n"

            # Sleep with small random jitter to avoid thundering herd
            sleep_time = FETCH_INTERVAL + random.uniform(-1.0, 1.0)
            await asyncio.sleep(max(1.0, sleep_time))


@app.get("/vehicles-sse")
async def vehicles_sse(request: Request):
    return StreamingResponse(
        vehicle_updates(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",           # important vs proxies
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.get("/")
async def root():
    return {
        "message": "RET Tram Tracker – production mode (updates ~every 8s)"
    }