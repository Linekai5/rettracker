<script>
  import { onMount, onDestroy } from 'svelte';
  import { initMap } from './map.js';
  import { browser } from '$app/environment';
  import { Vehicle } from './Vehicle.js';
  import retData from '$lib/assets/ret_network.json';
  import * as turf from '@turf/turf';

  let mapElement;
  let map;
  let evtSource;
  const vehicleState = new Map(); // Map<id, Vehicle>
  const routeGeometries = {}; // route_id -> MultiLineString

  // Mapping from internal RET Metro lineage to public identifiers
  const METRO_MAPPING = {
      "M006": "E",
      "M007": "C",
      "M008": "A",
      "M009": "B",
      "M010": "D"
  };

  function resolveGeometry(vData) {
      const rid = vData.route_id || "";
      const hint = vData.line_hint || "";
      
      // Priority 0: Metro Internal Code Mapping
      if (METRO_MAPPING[hint] && routeGeometries[METRO_MAPPING[hint]]) {
          return routeGeometries[METRO_MAPPING[hint]];
      }
      // Priority 1: Exact Match on Line Hint (e.g. "33" -> "33")
      if (hint && routeGeometries[hint]) {
          return routeGeometries[hint];
      }
      // Priority 2: Exact Match on Route ID
      if (routeGeometries[rid]) {
          return routeGeometries[rid];
      }
      
      // Priority 3: Fuzzy / Fallback via Spatial Discovery
      let minDist = Infinity;
      let bestRef = null;
      
      // Only do expensive spatial search if we have valid coordinates
      if (vData.lat && vData.lon && Math.abs(vData.lat) > 1) {
           const vPt = [vData.lon, vData.lat];
           for (const [ref, geom] of Object.entries(routeGeometries)) {
               const snapped = turf.nearestPointOnLine(geom, vPt);
               if (snapped && snapped.properties && snapped.properties.dist !== undefined) {
                   if (snapped.properties.dist < minDist) {
                       minDist = snapped.properties.dist;
                       bestRef = ref;
                   }
               }
           }
      }
      
      // Threshold: 0.1 km (100m) - slightly relaxed to catch drifts
      if (bestRef && minDist < 0.1) {
           return routeGeometries[bestRef];
      } 
      
      // Fuzzy Name Fallback
      for (const key of Object.keys(routeGeometries)) {
          if (rid === key || rid.includes(key) || key.includes(rid)) {
              if (Math.abs(rid.length - key.length) < 3) {
                   return routeGeometries[key];
              }
          }
      }
      
      return null;
  }


  async function startVehicleStream() {
      // Create SSE Connection
      const url = 'https://rettrack.dfelix.systems/vehicles-sse';
      console.log("Connecting to SSE:", url);

      evtSource = new EventSource(url);
      
      evtSource.onopen = () => {
          console.log("SSE Connection established successfully.");
      };
      
      evtSource.onmessage = (e) => {
          try {
              const payload = JSON.parse(e.data);
              if (payload.type === 'vehicles') {
                  payload.data.forEach(vData => {
                      if (vehicleState.has(vData.id)) {
                          const v = vehicleState.get(vData.id);
                          if (!v.hasRouteGeometry()) {
                                const geom = resolveGeometry(vData);
                                if (geom) v.setRouteGeometry(geom);
                          }
                          v.update(vData);
                      } else {
                          const routeGeom = resolveGeometry(vData);
                          const v = new Vehicle(vData, map, routeGeom);
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

    // 1. Pre-calculate Route Geometries (CPU Task)
    // Doing this synchronously before map init ensures lookups are ready immediately.
    const featureGroups = {};
    (retData.features || []).forEach(f => {
        if (f.geometry && f.geometry.type === 'LineString' && f.properties && f.properties.ref) {
            const ref = f.properties.ref;
            if (!featureGroups[ref]) featureGroups[ref] = [];
            featureGroups[ref].push(f.geometry.coordinates);
        }
    });
    for (const [ref, coords] of Object.entries(featureGroups)) {
        routeGeometries[ref] = turf.multiLineString(coords);
    }

    // 2. Initialize Map with Data (Critical Path Rendering)
    // Passing retData allows initMap to bake layers into the style spec, 
    // rendering lines instantly on init without waiting for 'load' event.
    map = await initMap(mapElement, retData);

    // 3. Inject Dependencies for Vehicles
    // Re-import maplibre to pass to Vehicle class (module is cached)
    const maplibreModule = await import('maplibre-gl');
    const maplibregl = maplibreModule.default || maplibreModule;
    Vehicle.injectLibrary(maplibregl);

    // 4. Start Stream Immediately
    // Do NOT wait for map 'load' event. API data should flow + markers create ASAP.
    // MapLibre handles DOM markers even if tiles aren't fully painted.
    startVehicleStream();
  });

  onDestroy(() => {
     if (evtSource) {
         evtSource.close();
     }
     vehicleState.forEach(v => v.remove());
     vehicleState.clear();
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
