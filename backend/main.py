from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from google.transit import gtfs_realtime_pb2 as gtfs
import httpx
import asyncio
import json
import os

app = FastAPI()

# Config
GTFS_VEHICLE_URL = os.getenv("GTFS_VEHICLE_URL", "https://gtfs.ovapi.nl/nl/vehiclePositions.pb")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "40"))   # seconden
SSE_INTERVAL = int(os.getenv("SSE_INTERVAL", "5"))      # seconden

cached_data = {"updates": [], "count": 0}

async def fetch_ovapi_data():
    global cached_data
    headers = {
        "User-Agent": "RET-Tracker-Backend",
        "Accept": "application/x-protobuf"
    }

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        while True:
            try:
                resp = await client.get(GTFS_VEHICLE_URL, headers=headers)
                if resp.status_code == 429:
                    print("Error 429: rate limit. Wacht 60s.")
                    await asyncio.sleep(60)
                    continue

                resp.raise_for_status()

                feed = gtfs.FeedMessage()
                feed.ParseFromString(resp.content)

                new_updates = []
                for entity in feed.entity:
                    if entity.HasField("vehicle") and entity.vehicle.HasField("position"):
                        v = entity.vehicle
                        route_id = v.trip.route_id

                        # Filter op RET; pas aan als je ook andere vervoerders wilt
                        if "RET" in route_id:
                            new_updates.append({
                                "id": entity.id,
                                "lat": v.position.latitude,
                                "lon": v.position.longitude,
                                "line": route_id.split(":")[-1],
                                "bearing": v.position.bearing if v.position.HasField("bearing") else 0
                            })

                cached_data = {"updates": new_updates, "count": len(new_updates)}
                print(f"Data updated: {len(new_updates)} RET vehicles found.")
            except Exception as e:
                print(f"Fetch error: {e}")

            await asyncio.sleep(POLL_INTERVAL)

@app.on_event("startup")
async def startup_event():
    # Start de fetch-task op de achtergrond
    asyncio.create_task(fetch_ovapi_data())

async def event_generator():
    while True:
        yield f"data: {json.dumps(cached_data)}\n\n"
        await asyncio.sleep(SSE_INTERVAL)

@app.get("/vehicles-sse")
async def vehicles_sse():
    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.get("/")
async def root():
    return {
        "status": "online",
        "ret_count": len(cached_data.get("updates", [])),
        "endpoint": "/vehicles-sse"
    }