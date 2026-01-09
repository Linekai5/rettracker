from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from google.transit import gtfs_realtime_pb2 as gtfs
import httpx
import asyncio
import json
import time

app = FastAPI()

current_vehicles = {}
last_fetch_time = 0
FETCH_INTERVAL = 5  # Elke 5s – snel!

async def vehicle_updates():
    global current_vehicles, last_fetch_time

    async with httpx.AsyncClient() as client:
        while True:
            updates = []
            current_time = time.time()

            if current_time - last_fetch_time > FETCH_INTERVAL:
                try:
                    response = await client.get("http://gtfs.ovapi.nl/nl/vehiclePositions.pb", timeout=10.0)
                    response.raise_for_status()

                    feed = gtfs.FeedMessage()
                    feed.ParseFromString(response.content)

                    new_vehicles = {}

                    for entity in feed.entity:
                        if entity.HasField('vehicle') and entity.vehicle.HasField('position'):
                            vehicle = entity.vehicle
                            trip = vehicle.trip
                            position = vehicle.position

                            if trip.route_id.startswith("RET"):
                                key = entity.id
                                line = trip.route_id.replace("RET:", "").replace("RET_", "")

                                vehicle_data = {
                                    "id": key,
                                    "lat": position.latitude,
                                    "lon": position.longitude,
                                    "line": line,
                                    "bearing": position.bearing or 0,
                                    "speed": position.speed or 0
                                }

                                new_vehicles[key] = vehicle_data

                                if key not in current_vehicles or current_vehicles[key] != vehicle_data:
                                    updates.append(vehicle_data)

                    current_vehicles = new_vehicles
                    last_fetch_time = current_time

                except Exception as e:
                    print("Fout:", e)

            if updates:
                yield f"data: {json.dumps({'updates': updates})}\n\n"
            else:
                yield ": heartbeat\n\n"  # Tegen Cloudflare timeout

            await asyncio.sleep(3)  # Check elke 3s

@app.get("/vehicles-sse")
async def vehicles_sse(request: Request):
    return StreamingResponse(vehicle_updates(), media_type="text/event-stream")

@app.get("/")
async def root():
    return {"message": "RET Tracker – snelle ovapi versie (5s updates)."}