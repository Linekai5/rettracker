<script>
  import { onMount, onDestroy } from 'svelte';
  import { initMap } from './map.js';
  import { browser } from '$app/environment';
  import retData from '$lib/assets/ret_network.json';

  let mapElement;
  let map;
  let evtSource;
  const vehicleState = new Map();

  function updateVehicleSource() {
      if (!map || !map.getSource('live-vehicles')) return;
      const features = Array.from(vehicleState.values());
      map.getSource('live-vehicles').setData({
          type: 'FeatureCollection',
          features: features
      });
  }

  function startVehicleStream() {
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
                  payload.data.forEach(v => {
                      vehicleState.set(v.id, {
                          type: 'Feature',
                          geometry: {
                              type: 'Point',
                              coordinates: [v.lon, v.lat]
                          },
                          properties: {
                              id: v.id,
                              rotation: v.bearing, // MapLibre uses 'icon-rotate' or similar, strict GeoJSON props
                              route_id: v.route_id,
                              speed: v.speed
                          }
                      });
                  });
                  updateVehicleSource();
              }
          } catch (err) {
              console.error("Error parsing SSE", err);
          }
      };

      evtSource.onerror = (err) => {
          console.error("SSE Error:", err);
          // Optional: Reconnect logic is naturally handled by EventSource usually, but strict error handling depends on browser
      };
  }

  onMount(async () => {
    if (!browser) return;

    map = await initMap(mapElement);

    map.on('load', () => {
       // --- BASE LAYERS (Static) ---
       map.addSource('ret-data', { type: 'geojson', data: retData });
       
       // --- LIVE SOURCE ---
       map.addSource('live-vehicles', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });

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
     if (browser && map) {
        map.remove();
     }
  });
</script>

<div bind:this={mapElement} class="map-container"></div>

<style>
  .map-container {
    width: 100%;
    height: 100%;
    position: absolute;
    top: 0;
    left: 0;
  }
</style>
