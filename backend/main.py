from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import httpx
import asyncio
import json
import time

app = FastAPI()

current_vehicles = {}
last_fetch_time = 0
CACHE_TIME = 1  # seconds

async def vehicle_updates():
    global current_vehicles, last_fetch_time

    async with httpx.AsyncClient(verify=False) as client:
        while True:
            updates = []
            now = time.time()

            if now - last_fetch_time > CACHE_TIME:
                try:
                    # 1. Get ALL journey keys
                    resp_keys = await client.get(
                        "https://v0.ovapi.nl/journey/",
                        timeout=10.0
                    )
                    resp_keys.raise_for_status()

                    journey_keys = [
                        k for k in resp_keys.json().keys()
                        if k.startswith("RET_")
                    ]

                    # 2. Load EVERY journey in ONE request
                    url = f"https://v0.ovapi.nl/journey/{','.join(journey_keys)}"
                    resp = await client.get(url, timeout=30.0)
                    resp.raise_for_status()

                    journeys = resp.json()
                    new_vehicles = {}

                    for journey_id, journey in journeys.items():
                        stops = journey.get("Stops", {})
                        for stop_id, stop in stops.items():
                            if stop.get("TripStopStatus") in ("DRIVING", "ARRIVED"):
                                vehicle = {
                                    "id": f"{journey_id}_{stop_id}",
                                    "lat": stop.get("latitude"),
                                    "lon": stop.get("longitude"),
                                    "line": stop.get("LinePublicNumber"),
                                    "bearing": stop.get("SideCode"),
                                    "speed": stop.get("Speed", 0),
                                    "type": stop.get("TransportType"),
                                    "destination": stop.get("DestinationName50"),
                                    "last_update": stop.get("LastUpdateTimeStamp"),
                                }

                                new_vehicles[vehicle["id"]] = vehicle

                                if (
                                    vehicle["id"] not in current_vehicles
                                    or current_vehicles[vehicle["id"]] != vehicle
                                ):
                                    updates.append(vehicle)

                    current_vehicles = new_vehicles
                    last_fetch_time = now

                except Exception as e:
                    yield f"data: {json.dumps({'error': str(e)})}\n\n"

            # Send SSE exactly like you expect
            if updates:
                yield f"data: {json.dumps({'updates': updates})}\n\n"

            await asyncio.sleep(0.5)  # keep stream alive

@app.get("/vehicles-sse")
async def vehicles_sse(request: Request):
    return StreamingResponse(
        vehicle_updates(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )

@app.get("/")
async def root():
    return {
        "message": "RET Tracker — all journeys loaded at once"
    }