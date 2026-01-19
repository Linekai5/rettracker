<script>
  import { onMount, onDestroy } from 'svelte';
  import { initMap } from './map.js';
  import { browser } from '$app/environment';
  import retData from '$lib/assets/ret_network.json';

  let mapElement;
  let map;

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
    });
  });

  onDestroy(() => {
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
