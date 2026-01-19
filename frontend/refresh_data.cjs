const fs = require('fs');
const path = require('path');
const osmtogeojson = require('osmtogeojson');

const OUTPUT_FILE = path.join(__dirname, 'src/lib/assets/ret_network.json');

const ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
];

// Whitelist of valid RET lines (Updated for 2024/2025 network changes)
const WHITELIST = new Set([
    // Metro
    'A','B','C','D','E',
    // Trams (New Network Structure)
    '1', // De Esch – Woudhoek/Holy
    '2', // Keizerswaard – Charlois
    '3', // Barendrecht – Rotterdam Centraal
    '4', // Molenlaan – Heemraadsplein
    '5', // Beverwaard – Rotterdam Centraal
    '6', // Het Lage Land – Marconiplein
    '7', // Woudestein – Willemsplein
    '8', // Schiebroek – Spangen
    // Legacy/TramPlus (Kept just in case, though user says they don't exist)
    '20','21','23','24','25' 
]);

// RET Metro Colors (Official or Standard approx)
const METRO_COLORS = {
    'A': '#1ea245', // Green
    'B': '#ffce00', // Yellow
    'C': '#db002e', // Red
    'D': '#00a1de', // Light Blue
    'E': '#003e83'  // Dark Blue
};

// Bounding Box: Rotterdam Area
// 51.8 to 52.03 Lat, 4.3 to 4.7 Lon
const QUERY = `
    [out:json][timeout:90];
    (
      relation["route"~"subway|tram|light_rail"](51.8,4.3,52.03,4.7);
    );
    out geom;
`;

async function fetchWithRetry() {
    for (const endpoint of ENDPOINTS) {
        console.log(`Trying endpoint: ${endpoint}`);
        try {
            const controller = new AbortController();
            const id = setTimeout(() => controller.abort(), 90000); 

            const response = await fetch(endpoint, {
                method: 'POST',
                body: `data=${encodeURIComponent(QUERY)}`,
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                signal: controller.signal
            });
            clearTimeout(id);

            if (!response.ok) {
                const text = await response.text();
                console.warn(`Failed: ${response.status} ${response.statusText}`);
                continue;
            }

            const data = await response.json();
            console.log(`Success! Received ${data.elements ? data.elements.length : 0} elements.`);
            return data;
        } catch (err) {
            console.warn(`Error connecting to ${endpoint}: ${err.message}`);
        }
    }
    throw new Error("All endpoints failed.");
}

async function main() {
    try {
        console.log("Fetching Rotterdam Trams & Metro (Geographic Search)...");
        const rawOsmData = await fetchWithRetry();

        console.log("Converting to GeoJSON...");
        const geojson = osmtogeojson(rawOsmData);

        const initialCount = geojson.features.length;
        const validFeatures = [];

        for (const f of geojson.features) {
            const p = f.properties || {};
            const ref = String(p.ref).toUpperCase();
            
            // 1. Whitelist Check
            if (!p.ref || !WHITELIST.has(ref)) continue;
            
            // 2. Additional validity checks (exclude known non-RET operators if leaked)
            if (p.operator && p.operator.includes('HTM')) continue;

            // 3. Post-Processing for MapComponent.svelte
            // MapComponent expects: 'layer' (metro/tram), 'color' (for metro)
            
            let layer = null;
            if (p.route === 'subway' || p.route === 'light_rail') layer = 'metro';
            else if (p.route === 'tram') layer = 'tram';
            
            if (!layer) continue;
            
            p.layer = layer;

            // Assign Colors
            if (layer === 'metro') {
                p.color = METRO_COLORS[ref] || '#000000'; 
            }
            
            // 4. Simplify Properties (Speed Optimization)
            // Remove huge usage of tags we don't render
            const keepProps = {
                'layer': p.layer,
                'ref': p.ref,
                'route': p.route,
                'color': p.color
            };
            f.properties = keepProps;

            validFeatures.push(f);
        }

        geojson.features = validFeatures;

        console.log(`Filtered and Processed ${initialCount} -> ${geojson.features.length} features.`);
        console.log(`Writing to ${OUTPUT_FILE}`);
        // Minify output (no spaces/indentation) for faster loading
        fs.writeFileSync(OUTPUT_FILE, JSON.stringify(geojson));
        console.log("Done.");

    } catch (error) {
        console.error("Critical Error:", error.message);
        process.exit(1);
    }
}

main();
