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
FETCH_INTERVAL = 8.0  # Elke 8 seconden – snel en veilig
BATCH_SIZE = 40       # Veilige batch grootte voor /journey/ calls

async def vehicle_updates():
    global current_vehicles, last_fetch_time

    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
        while True:
            updates = []
            now = time.time()

            if now - last_fetch_time >= FETCH_INTERVAL:
                try:
                    # Stap 1: Haal alle actieve RET journeys op
                    resp_keys = await client.get("https://v0.ovapi.nl/journey/")
                    resp_keys.raise_for_status()

                    journey_keys = [k for k in resp_keys.json().keys() if k.startswith("RET_")]

                    if not journey_keys:
                        print("Geen RET journeys gevonden")
                        await asyncio.sleep(2.0)
                        continue

                    new_vehicles = {}
                    print(f"Haal {len(journey_keys)} RET journeys op...")

                    # Stap 2: Batch fetch (om rate limit te voorkomen)
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

                                    # Alleen tram, metro, bus (je kunt dit aanpassen)
                                    if transport_type not in {"TRAM", "METRO", "BUS"}:
                                        continue

                                    # Check of het een actieve rit is
                                    if stop.get("TripStopStatus") in ("DRIVING", "ARRIVED", "DEPARTING"):
                                        vehicle = {
                                            "id": f"{journey_id}_{stop_id}",
                                            "lat": stop.get("latitude"),
                                            "lon": stop.get("longitude"),
                                            "line": stop.get("LinePublicNumber"),
                                            "bearing": stop.get("SideCode", 0),  # richting (bearing)
                                            "speed": stop.get("Speed", 0),
                                            "type": transport_type,               # TRAM, METRO, BUS
                                            "destination": stop.get("DestinationName50"),
                                            "last_update": stop.get("LastUpdateTimeStamp"),  # Timestamp!
                                            "delay": stop.get("DelayInSeconds", 0),
                                            "direction": stop.get("Direction"),
                                        }

                                        new_vehicles[vehicle["id"]] = vehicle

                                        # Alleen als nieuw of veranderd → stuur update
                                        if (
                                            vehicle["id"] not in current_vehicles
                                            or current_vehicles[vehicle["id"]] != vehicle
                                        ):
                                            updates.append(vehicle)

                        except Exception as e:
                            print(f"Batch mislukt: {url} → {str(e)}")

                        await asyncio.sleep(0.15)  # Kleine pauze tussen batches

                    # Update globale cache
                    current_vehicles = new_vehicles
                    last_fetch_time = now
                    print(f"Update klaar - {len(updates)} voertuigen veranderd")

                except Exception as e:
                    print(f"Hoofd fetch fout: {str(e)}")

            # Stuur updates als er iets is
            if updates:
                yield f"data: {json.dumps({'updates': updates})}\n\n"

            # Kleine random jitter om throttling te voorkomen
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
            "X-Accel-Buffering": "no",  # Belangrijk tegen proxies/Cloudflare
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.get("/")
async def root():
    return {
        "message": "RET Tram/Metro/Bus Tracker – live elke ~8s"
    }