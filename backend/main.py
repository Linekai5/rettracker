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
    socket.setsockopt_string(zmq.SUBSCRIBE, "")  # Alles ontvangen

    print("Verbonden met KV78turbo – wacht op berichten...")

    while True:
        try:
            message = socket.recv(flags=zmq.NOBLOCK)  # Non-blocking voor async
        except zmq.Again:
            await asyncio.sleep(0.01)
            continue

        try:
            root = ET.fromstring(message)

            updates = []

            for posinfo in root.findall('.//POSINFO'):
                dataowner = posinfo.find('dataownercode')
                if dataowner is not None and dataowner.text == "RET":
                    lat_elem = posinfo.find('latitude')
                    lon_elem = posinfo.find('longitude')
                    bearing_elem = posinfo.find('bearing')
                    line_elem = posinfo.find('lineplanningnumber') or posinfo.find('linenumber') or posinfo.find('journeyNumber')

                    if lat_elem is not None and lon_elem is not None:
                        key = posinfo.find('vehiclenumber').text or str(hash(message))

                        vehicle_data = {
                            "id": key,
                            "lat": float(lat_elem.text),
                            "lon": float(lon_elem.text),
                            "line": line_elem.text if line_elem is not None else "?",
                            "bearing": float(bearing_elem.text) if bearing_elem is not None else 0
                        }

                        if key not in current_vehicles or current_vehicles[key] != vehicle_data:
                            updates.append(vehicle_data)
                            current_vehicles[key] = vehicle_data

            if updates:
                yield f"data: {json.dumps({'updates': updates})}\n\n"

        except Exception as e:
            print("Parse error (normaal bij sommige berichten):", e)
            # Geen yield – voorkomt invalid token

@app.get("/vehicles-sse")
async def vehicles_sse(request: Request):
    return StreamingResponse(kv78turbo_stream(), media_type="text/event-stream")

@app.get("/")
async def root():
    return {"message": "KV78turbo RET Tracker draait! Bijna 0 delay push."}