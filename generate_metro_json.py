import json

# Approximate coordinates for major stations
stations = {
    "Beurs": [4.482, 51.918],
    "Rotterdam Centraal": [4.4777, 51.9242],
    "Den Haag Centraal": [4.3245, 52.0809],
    "Nesselande": [4.5878, 51.9824],
    "Hoek van Holland Strand": [4.1070, 51.9862],
    "Slinge": [4.4775, 51.8743],
    "De Akkers": [4.3283, 51.8427],
    "De Terp": [4.583, 51.935],
    "Binnenhof": [4.556, 51.954],
    "Vlaardingen West": [4.313, 51.910],
    "Schiedam Centrum": [4.394, 51.924], # hub for A, B, C
    "Capelsebrug": [4.545, 51.917], # hub for A, B, C
    "Zuidplein": [4.488, 51.886], # hub for D, E
}

# Define lines as sequences of key stations
lines = [
    {
        "line": "A",
        "color": "green", 
        "route": ["Binnenhof", "Capelsebrug", "Beurs", "Schiedam Centrum", "Vlaardingen West"]
    },
    {
        "line": "B",
        "color": "yellow",
        "route": ["Nesselande", "Capelsebrug", "Beurs", "Schiedam Centrum", "Hoek van Holland Strand"]
    },
    {
        "line": "C",
        "color": "red",
        "route": ["De Terp", "Capelsebrug", "Beurs", "Schiedam Centrum", "De Akkers"]
    },
    {
        "line": "D",
        "color": "cyan",
        "route": ["Rotterdam Centraal", "Beurs", "Zuidplein", "De Akkers"]
    },
    {
        "line": "E",
        "color": "blue",
        "route": ["Den Haag Centraal", "Rotterdam Centraal", "Beurs", "Zuidplein", "Slinge"]
    }
]

geojson = {
    "type": "FeatureCollection",
    "features": []
}

for l in lines:
    coords = [stations[name] for name in l["route"]]
    feature = {
        "type": "Feature",
        "properties": {
            "line": l["line"],
            "color": l["color"],
        },
        "geometry": {
            "type": "LineString",
            "coordinates": coords
        }
    }
    geojson["features"].append(feature)

# Add stations as points
for name, coords in stations.items():
    geojson["features"].append({
        "type": "Feature",
        "properties": {
            "name": name,
            "type": "station"
        },
        "geometry": {
            "type": "Point",
            "coordinates": coords
        }
    })

with open('frontend/static/metro_lines.json', 'w') as f:
    json.dump(geojson, f, indent=2)

print("Created frontend/static/metro_lines.json")
