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

// Whitelist of valid RET TRAM/METRO lines
// Buses are filtered by OPERATOR name, not whitelist (too many lines)
const WHITELIST_TRAM_METRO = new Set([
    // Metro
    'A','B','C','D','E',
    // Trams
    '1','2','3','4','5','6','7','8',
    '20','21','23','24','25'
]);

// RET Metro Colors
const METRO_COLORS = {
    'A': '#1ea245', // Green
    'B': '#ffce00', // Yellow
    'C': '#db002e', // Red
    'D': '#00a1de', // Light Blue
    'E': '#003e83'  // Dark Blue
};

// Bounding Box: Rotterdam Area
const QUERY = `
    [out:json][timeout:90];
    (
      relation["route"~"subway|tram|light_rail|bus"](51.8,4.3,52.03,4.7);
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
        console.log("Fetching Rotterdam Public Transport (Geographic Search)...");
        const rawOsmData = await fetchWithRetry();

        console.log("Converting to GeoJSON...");
        const geojson = osmtogeojson(rawOsmData);

        const initialCount = geojson.features.length;
        const validFeatures = [];

        for (const f of geojson.features) {
            const p = f.properties || {};
            const ref = String(p.ref || '').toUpperCase();
            const route = p.route;

            // 1. Determine Layer
            let layer = null;
            if (route === 'subway' || route === 'light_rail') layer = 'metro';
            else if (route === 'tram') layer = 'tram';
            else if (route === 'bus') layer = 'bus';
            
            if (!layer) continue;

            // 2. Filter Logic
            if (layer === 'metro' || layer === 'tram') {
                // Tram/Metro: Strict Whitelist (Fixes Den Hague leakage)
                if (!p.ref || !WHITELIST_TRAM_METRO.has(ref)) continue;
                if (p.operator && p.operator.includes('HTM')) continue;
            } 
            else if (layer === 'bus') {
                // Bus: Strict Operator Check (Fixes random buses)
                // Must be RET.
                const op = (p.operator || '').toUpperCase();
                // Allow "RET", "Rotterdam...", "Stichting...", but generally RET is standard tag.
                if (op !== 'RET' && !op.includes('ROTTERDAMSE ELEKTRISCHE')) {
                     // Check network tag as fallback
                     const net = (p.network || '').toUpperCase();
                     if (net !== 'RET' && !net.includes('ROTTERDAM')) continue;
                }
            }

            p.layer = layer;

            // 3. Assign Colors
            if (layer === 'metro') {
                p.color = METRO_COLORS[ref] || '#000000'; 
            }
            // Trams get purple in Svelte.
            // Buses get grey in Svelte.

            // 4. Cleanup
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
        fs.writeFileSync(OUTPUT_FILE, JSON.stringify(geojson));
        console.log("Done.");

    } catch (error) {
        console.error("Critical Error:", error.message);
        process.exit(1);
    }
}

main();
