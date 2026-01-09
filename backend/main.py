from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import zmq
import xml.etree.ElementTree as ET
import asyncio
import json

app = FastAPI()

current_vehicles = {}

async def ret_vehicles_stream():
    global current_vehicles

    context = zmq.Context()
    socket = context.socket(zmq.SUB)
    socket.connect("tcp://pubsub.besteffort.ndovloket.nl:7658")  # KV6 port voor POSINFO
    socket.setsockopt_string(zmq.SUBSCRIBE, "/RET/KV6posinfo")  # Alleen RET POSINFO

    print("Verbonden met NDOV KV6 – subscribed op /RET/KV6posinfo – wacht op RET data...")

    while True:
        try:
            message = socket.recv(flags=zmq.NOBLOCK)
        except zmq.Again:
            await asyncio.sleep(0.01)
            continue

        try:
            root = ET.fromstring(message)

            updates = []

            for posinfo in root.findall('.//KV6posinfo'):
                lat = posinfo.find('latitude')
                lon = posinfo.find('longitude')
                bearing = posinfo.find('bearing')
                line = posinfo.find('lineplanningnumber')

                if lat is not None and lon is not None:
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
            pass  # Skip invalid

@app.get("/vehicles-sse")
async def vehicles_sse(request: Request):
    return StreamingResponse(ret_vehicles_stream(), media_type="text/event-stream")

@app.get("/")
async def root():
    return {"message": "NDOV KV6 RET Tracker draait – live push!"}