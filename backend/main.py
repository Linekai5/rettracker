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
    socket.setsockopt_string(zmq.SUBSCRIBE, "")  # Subscribe op alles

    while True:
        try:
            message = socket.recv()  # Blokkeert tot nieuw bericht
            root = ET.fromstring(message)

            updates = []

            for posinfo in root.findall('.//POSINFO'):
                dataowner = posinfo.find('dataownercode')
                if dataowner is not None and dataowner.text == "RET":
                    lat = posinfo.find('latitude').text if posinfo.find('latitude') is not None else None
                    lon = posinfo.find('longitude').text if posinfo.find('longitude') is not None else None
                    bearing = posinfo.find('bearing').text or "0"
                    line = posinfo.find('linenumber').text or posinfo.find('lineplanningnumber').text or "?"

                    if lat and lon:
                        key = posinfo.find('vehiclenumber').text or str(hash(message))

                        vehicle_data = {
                            "id": key,
                            "lat": float(lat),
                            "lon": float(lon),
                            "line": line,
                            "bearing": float(bearing)
                        }

                        if key not in current_vehicles or current_vehicles[key] != vehicle_data:
                            updates.append(vehicle_data)
                            current_vehicles[key] = vehicle_data

            if updates:
                yield f"data: {json.dumps({'updates': updates})}\n\n"

        except Exception as e:
            print("Error:", e)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

        await asyncio.sleep(0.01)  # Kleine sleep voor andere tasks

@app.get("/vehicles-sse")
async def vehicles_sse(request: Request):
    return StreamingResponse(kv78turbo_stream(), media_type="text/event-stream")

@app.get("/")
async def root():
    return {"message": "KV78turbo backend draait! Live push van RET voertuigen."}