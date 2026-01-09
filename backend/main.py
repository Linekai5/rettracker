from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import zmq
import xml.etree.ElementTree as ET
import asyncio
import json

app = FastAPI()

current_vehicles = {}

async def kv78turbo_stream():
    global current_vehicles

    context = zmq.Context()
    socket = context.socket(zmq.SUB)
    socket.connect("tcp://pubsub.besteffort.ndovloket.nl:7817")
    socket.setsockopt_string(zmq.SUBSCRIBE, "")

    print("Verbonden met KV78turbo – wacht op POSINFO berichten...")

    while True:
        try:
            message = socket.recv(flags=zmq.NOBLOCK)
        except zmq.Again:
            await asyncio.sleep(0.01)
            continue

        # Alleen echte XML berichten verwerken
        if not message or not message.lstrip().startswith(b'<'):
            continue

        try:
            root = ET.fromstring(message)

            updates = []

            # Alleen POSINFO berichten verwerken (KV6 voertuigposities)
            for posinfo in root.findall('.//POSINFO'):
                dataowner = posinfo.find('dataownercode')
                if dataowner is not None and dataowner.text == "RET":
                    lat = posinfo.find('latitude')
                    lon = posinfo.find('longitude')
                    bearing = posinfo.find('bearing')
                    line = posinfo.find('lineplanningnumber') or posinfo.find('linenumber')

                    if lat is not None and lon is not None and lat.text and lon.text:
                        key = posinfo.find('vehiclenumber').text or "unknown"

                        vehicle_data = {
                            "id": key,
                            "lat": float(lat.text),
                            "lon": float(lon.text),
                            "line": line.text if line is not None else "?",
                            "bearing": float(bearing.text) if bearing is not None and bearing.text else 0
                        }

                        if key not in current_vehicles or current_vehicles[key] != vehicle_data:
                            updates.append(vehicle_data)
                            current_vehicles[key] = vehicle_data

            if updates:
                print(f"RET update: {len(updates)} voertuigen")
                yield f"data: {json.dumps({'updates': updates})}\n\n"

        except Exception as e:
            # Geen yield bij error – voorkomt "invalid token" in frontend
            pass

@app.get("/vehicles-sse")
async def vehicles_sse(request: Request):
    return StreamingResponse(kv78turbo_stream(), media_type="text/event-stream")

@app.get("/")
async def root():
    return {"message": "KV78turbo RET Tracker draait – live push met bijna 0 delay."}