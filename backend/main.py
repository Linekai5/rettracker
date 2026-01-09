from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from google.transit import gtfs_realtime_pb2 as gtfs
import httpx
import asyncio
import json

app = FastAPI()

# Globale cache om de server te ontzien
cached_data = {"updates": []}

async def fetch_ovapi_data():
    global cached_data
    async with httpx.AsyncClient() as client:
        while True:
            try:
                # Gebruik een User-Agent, sommige servers blokkeren Python-requests zonder header
                headers = {"User-Agent": "RET-Tracker-App-v1.0"}
                response = await client.get("http://gtfs.ovapi.nl/nl/vehiclePositions.pb", timeout=15.0, headers=headers)
                
                if response.status_code == 429:
                    print("Gecapped door OVAPI. Wachten...")
                    await asyncio.sleep(60) # Wacht een minuut bij 429
                    continue

                response.raise_for_status()
                feed = gtfs.FeedMessage()
                feed.ParseFromString(response.content)

                new_updates = []
                for entity in feed.entity:
                    if entity.HasField('vehicle') and entity.vehicle.HasField('position'):
                        vehicle = entity.vehicle
                        if "RET" in vehicle.trip.route_id:
                            new_updates.append({
                                "id": entity.id,
                                "lat": vehicle.position.latitude,
                                "lon": vehicle.position.longitude,
                                "line": vehicle.trip.route_id.replace("RET:", ""),
                            })
                
                cached_data["updates"] = new_updates
                print(f"Cache ververst: {len(new_updates)} voertuigen.")

            except Exception as e:
                print(f"Fout: {e}")

            # BELANGRIJK: Niet vaker dan elke 30-60 seconden voor publieke feeds!
            await asyncio.sleep(45)

@app.on_event("startup")
async def startup_event():
    # Start de data-fetcher op de achtergrond zodra de server start
    asyncio.create_task(fetch_ovapi_data())

async def event_generator():
    while True:
        # Stuur de huidige cache naar de client
        yield f"data: {json.dumps(cached_data)}\n\n"
        await asyncio.sleep(5) # De client krijgt elke 5s een update uit de cache

@app.get("/vehicles-sse")
async def vehicles_sse():
    return StreamingResponse(event_generator(), media_type="text/event-stream")
