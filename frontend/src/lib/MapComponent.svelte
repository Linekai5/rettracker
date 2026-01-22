<script>
  import { onMount, onDestroy } from 'svelte';
  import { initMap } from './map.js';
  import { browser } from '$app/environment';
  import { Vehicle } from './Vehicle.js';
  import retData from '$lib/assets/ret_network.json';

  let mapElement;
  let map;
  let evtSource;
  const vehicleState = new Map(); // Map<id, Vehicle>

  async function startVehicleStream() {
      // Ensure library is loaded for Vehicle class
      const maplibreModule = await import('maplibre-gl');
      const maplibregl = maplibreModule.default || maplibreModule;
      Vehicle.injectLibrary(maplibregl);

      // If dev, might want localhost, but user set VITE_API_URL in .env
      const baseUrl = import.meta.env.VITE_API_URL || 'http://localhost:8000';
      const url = `${baseUrl}/vehicles-sse`;
      console.log("Connecting to SSE:", url);

      evtSource = new EventSource(url);
      
      evtSource.onopen = () => {
          console.log("SSE Connection established successfully.");
      };
      
      evtSource.onmessage = (e) => {
          try {
              const payload = JSON.parse(e.data);
              if (payload.type === 'vehicles') {
                  // Payload data is a list of vehicles
                  // We process each one
                  payload.data.forEach(vData => {
                      if (vehicleState.has(vData.id)) {
                          // Update existing
                          vehicleState.get(vData.id).update(vData);
                      } else {
                          // Create new
                          const v = new Vehicle(vData, map);
                          vehicleState.set(vData.id, v);
                      }
                  });
              }
          } catch (err) {
              console.error("Error parsing SSE", err);
          }
      };

      evtSource.onerror = (err) => {
          console.error("SSE Error:", err);
      };
  }

  onMount(async () => {
    if (!browser) return;

    map = await initMap(mapElement);

    map.on('load', () => {
       // --- BASE LAYERS (Static) ---
       map.addSource('ret-data', { type: 'geojson', data: retData });
       
       // 1. Bus Layer
       map.addLayer({
         id: 'ret-bus', type: 'line', source: 'ret-data',
         filter: ['==', 'layer', 'bus'],
         layout: { 'line-join': 'round', 'line-cap': 'round' },
         paint: {
           'line-color': '#D3D3D3', 
           'line-width': ['interpolate', ['linear'], ['zoom'], 10, 0.5, 14, 1.5],
           'line-opacity': 0.6
         }
       });

       // 2. Tram Layer
       map.addLayer({
         id: 'ret-tram', type: 'line', source: 'ret-data',
         filter: ['==', 'layer', 'tram'],
         layout: { 'line-join': 'round', 'line-cap': 'round' },
         paint: {
           'line-color': '#D100AA',
           'line-width': ['interpolate', ['linear'], ['zoom'], 10, 0.5, 14, 1.5],
           'line-opacity': 0.9
         }
       });

       // 3. Metro Layer
       map.addLayer({
         id: 'ret-metro', type: 'line', source: 'ret-data',
         filter: ['==', 'layer', 'metro'],
         layout: { 'line-join': 'round', 'line-cap': 'round' },
         paint: {
           'line-color': ['get', 'color'],
           'line-width': ['interpolate', ['linear'], ['zoom'], 10, 1.5, 14, 3],
           'line-opacity': 1.0
         }
       });

       // 4. Stops Layer
       map.addLayer({
         id: 'ret-stops', type: 'circle', source: 'ret-data',
         filter: ['has', 'isStop'],
         paint: {
           'circle-color': '#ffffff',
           'circle-radius': ['interpolate', ['linear'], ['zoom'], 10, 2, 14, 4],
           'circle-stroke-width': 1.5,
           'circle-stroke-color': '#000000',
         }
       });

       // 5. Live Vehicles Layer
       map.addLayer({
         id: 'ret-vehicles', type: 'circle', source: 'live-vehicles',
         paint: {
           'circle-color': '#ff0000',
           'circle-radius': ['interpolate', ['linear'], ['zoom'], 10, 3, 14, 6],
           'circle-stroke-width': 1,
           'circle-stroke-color': '#ffffff',
         }
       });
       
       startVehicleStream();
    });
  });

  onDestroy(() => {
     if (evtSource) {
         evtSource.close();
     }
     vehicleState.forEach(v => v.remove());
     vehicleState.clear();
dth: 100%;
    height: 100%;
    position: absolute;
    top: 0;
    left: 0;
  }
</style>
