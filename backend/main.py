from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from google.transit import gtfs_realtime_pb2 as gtfs
import httpx, asyncio, json, os

app = FastAPI()

GTFS_VEHICLE_URL = os.getenv("GTFS_VEHICLE_URL", "https://gtfs.ovapi.nl/nl/vehiclePositions.pb")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "40"))
SSE_INTERVAL = int(os.getenv("SSE_INTERVAL", "5"))

cached_data = {"updates": [], "count": 0, "entities": 0, "debug_sample": []}

async def fetch_ovapi_data():
    global cached_data
    headers = {"User-Agent": "RET-Tracker-Backend", "Accept": "application/x-protobuf"}

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        while True:
            try:
                resp = await client.get(GTFS_VEHICLE_URL, headers=headers)
                print("HTTP", resp.status_code, "bytes", len(resp.content))
                if resp.status_code == 429:
                    print("Rate limit 429 → 60s pauze")
                    await asyncio.sleep(60)
                    continue
                resp.raise_for_status()

                feed = gtfs.FeedMessage()
                feed.ParseFromString(resp.content)
                entities_total = len(feed.entity)
                print("Entities in feed:", entities_total)

                new_updates = []
                sample = []
                for entity in feed.entity:
                    if entity.HasField("vehicle") and entity.vehicle.HasField("position"):
                        v = entity.vehicle
                        route_id = v.trip.route_id
                        # Tijdelijk GEEN filter om te zien of er iets is:
                        new_updates.append({
                            "id": entity.id,
                            "lat": v.position.latitude,
                            "lon": v.position.longitude,
                            "line": route_id,
                            "bearing": v.position.bearing if v.position.HasField("bearing") else 0
                        })
                        if len(sample) < 5:
                            sample.append({"id": entity.id, "route": route_id})
                cached_data = {
                    "updates": new_updates,
                    "count": len(new_updates),
                    "entities": entities_total,
                    "debug_sample": sample,
                }
                print(f"18:19 Data updated: {len(new_updates)} vehicles (entities: {entities_total}). Sample: {sample}")
            except Exception as e:
                print("Fetch error:", e)
            await asyncio.sleep(POLL_INTERVAL)

@app.on_event("startup")
async def startup_event():
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
    return cached_data