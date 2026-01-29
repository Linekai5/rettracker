
import httpx
from google.transit import gtfs_realtime_pb2
import sys

URL = "http://gtfs.ovapi.nl/nl/vehiclePositions.pb"

def fetch_and_inspect():
    print(f"Fetching {URL}...")
    try:
        response = httpx.get(URL, timeout=30) # Increased timeout for larger file
        print(f"Status Code: {response.status_code}")
        print(f"Content Length: {len(response.content)} bytes")
        
        if response.status_code != 200:
            print("Failed to fetch feed.")
            return

        feed = gtfs_realtime_pb2.FeedMessage()
        try:
            feed.ParseFromString(response.content)
            print("Protobuf parsed successfully.")
            print(f"Number of entities: {len(feed.entity)}")
            
            count = 0
            bbox_min_lat, bbox_max_lat = 51.5, 52.3
            bbox_min_lon, bbox_max_lon = 3.8, 4.9

            in_bbox = 0
            ret_explicit = 0

            for entity in feed.entity:
                if entity.HasField('vehicle'):
                    v = entity.vehicle
                    pos = v.position
                    
                    if not pos.latitude: continue

                    if (bbox_min_lat <= pos.latitude <= bbox_max_lat and 
                        bbox_min_lon <= pos.longitude <= bbox_max_lon):
                        
                        in_bbox += 1
                        if in_bbox <= 10:
                            print(f"[Rotterdam] Trip: {v.trip.trip_id}, Route: {v.trip.route_id}, Label: {v.vehicle.label}")
                        
                        # Check for RET string
                        # if "RET" in str(v):
                        #     ret_explicit += 1

                        label = v.vehicle.label
                        v_type = "bus"
                        try:
                            if label.isdigit():
                                num = int(label)
                                if 5000 <= num <= 5800: v_type = "metro"
                                elif 2000 <= num <= 2200: v_type = "tram"
                        except: pass
                        
                        if in_bbox <= 10:
                            print(f"[Rotterdam] Label: {label} -> Inferred: {v_type}")

            print(f"Total vehicles: {len(feed.entity)}")
            print(f"Vehicles in Rotterdam BBox: {in_bbox}")

        except Exception as e:
            print(f"Protobuf Parse Error: {e}")

    except Exception as e:
        print(f"Network Error: {e}")

if __name__ == "__main__":
    fetch_and_inspect()
