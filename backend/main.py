from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from google.transit import gtfs_realtime_pb2 as gtfs
import httpx
import asyncio
import json

app = FastAPI()

# Globale cache
cached_data = {"updates": []}

async def fetch_ovapi_data():
    global cached_data
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            try:
                headers = {"User-Agent": "RET-Tracker-Backend"}
                # De volledige URL inclusief protocol en pad
                url = "http://gtfs.ovapi.nl"
                response = await client.get(url, headers=headers)
                
                if response.status_code == 429:
                    print("Error 429: Rate limit. Waiting 60s.")
                    await asyncio.sleep(60)
                    continue

                response.raise_for_status()
                
                feed = gtfs.FeedMessage()
                feed.ParseFromString(response.content)

                new_updates = []
                for entity in feed.entity:
                    if entity.HasField('vehicle') and entity.vehicle.HasField('position'):
                        v = entity.vehicle
                        route_id = v.trip.route_id
                        
                        # Filter op RET voertuigen
                        if "RET" in route_id:
                            new_updates.append({
                                "id": entity.id,
                                "lat": v.position.latitude,
                                "lon": v.position.longitude,
                                "line": route_id.split(':')[-1],
                                "bearing": v.position.bearing if v.position.HasField('bearing') else 0
                            })
                
                cached_data = {"updates": new_updates, "count": len(new_updates)}
                print(f"Data updated: {len(new_updates)} RET vehicles found.")

            except Exception as e:
                print(f"Fetch error: {e}")

            # Wacht 40 seconden voor de volgende update
            await asyncio.sleep(40)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(fetch_ovapi_data())

async def event_generator():
    while True:
        yield f"data: {json.dumps(cached_data)}\n\n"
        await asyncio.sleep(5)

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
