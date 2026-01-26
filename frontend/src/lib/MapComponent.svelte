<script>
  import { onMount, onDestroy } from 'svelte';
  import { initMap } from './map.js';
  import { browser } from '$app/environment';
  import { Vehicle } from './Vehicle.js';
  // Removed direct import to avoid bundling 2.4MB JSON
  // import retData from '$lib/assets/ret_network.json';
  import * as turf from '@turf/turf';

  let mapElement;
  let map;
  let sseConnections = { metro: null, tram: null, bus: null };
  let selectedId = null;
  let focusInterval = null;
  
  // Filter state
  let enabledTypes = {
      tram: false,
      metro: false,
      bus: false
  };

  const vehicleState = new Map(); // Map<id, Vehicle>
  const routeGeometries = {}; // route_id -> MultiLineString
  const resolvedCache = new Map(); // rid + hint -> geometry
  
  // Use relative URLs if in production, or localhost during development
  // This avoids 404s if the frontend is hit from a different domain or port
  const API_BASE = (browser && (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1')) 
      ? 'http://localhost:8000' 
      : 'https://rettrack.dfelix.systems';

  // Mapping from internal RET Metro lineage to public identifiers
  const METRO_MAPPING = {
      "M006": "E",
      "M007": "C",
      "M008": "A",
      "M009": "B",
      "M010": "D"
  };

  const VehicleTypeColors = {
      bus: '#808080',
      tram: '#D100AA',
      metro: '#00a1de' // Will be overridden by specific metro color if matched
  };

  function resolveGeometry(vData) {
      if (!vData || !vData.lat || !vData.lon) return null;
      
      const rid = vData.route_id || "";
      const hint = vData.line_hint || "";
      const cacheKey = `${rid}_${hint}`;
      
      if (resolvedCache.has(cacheKey)) return resolvedCache.get(cacheKey);

      let found = null;
      let minDist = Infinity;
      let bestRef = null;
      const vPt = [vData.lon, vData.lat];
      
      // -- Step 1: Exact Matches (Most Accurate) --
      if (hint && routeGeometries[hint]) {
          found = routeGeometries[hint];
      } else if (rid && routeGeometries[rid]) {
          found = routeGeometries[rid];
      } else if (METRO_MAPPING[hint] && routeGeometries[METRO_MAPPING[hint]]) {
          found = routeGeometries[METRO_MAPPING[hint]];
      }
      
      // -- Step 2: Spatial Match (Search within names first) --
      // If we have a hint like "25", only search lines including "25" for best performance and accuracy
      if (!found && hint) {
          for (const [ref, geom] of Object.entries(routeGeometries)) {
              if (ref.includes(hint) || hint.includes(ref)) {
                  try {
                      const snapped = turf.nearestPointOnLine(geom, vPt);
                      if (snapped && snapped.properties.dist < minDist) {
                          minDist = snapped.properties.dist;
                          bestRef = ref;
                      }
                  } catch(e) {}
              }
          }
          if (bestRef && minDist < 0.5) found = routeGeometries[bestRef];
      }
      
      // -- Step 3: Global Spatial Search (Aggressive Clipping) --
      // If still not found, search the ENTIRE network for the closest line within 1km
      if (!found) {
          minDist = Infinity;
          bestRef = null;
          for (const [ref, geom] of Object.entries(routeGeometries)) {
              try {
                  const snapped = turf.nearestPointOnLine(geom, vPt);
                  if (snapped && snapped.properties.dist < minDist) {
                      minDist = snapped.properties.dist;
                      bestRef = ref;
                  }
              } catch(e) {}
          }
          // Restore 1km threshold for total network clipping
          if (bestRef && minDist < 1.0) {
              found = routeGeometries[bestRef];
          }
      }
      
      if (found) {
        resolvedCache.set(cacheKey, found);
      }
      return found;
  }

  async function fetchVehicleUpdate(id) {
      if (!id) return;
      try {
          const response = await fetch(`${API_BASE}/vehicles/${id}`);
          if (response.ok) {
              const vData = await response.json();
              if (vData.id && vehicleState.has(vData.id)) {
                  const v = vehicleState.get(vData.id);
                  if (!v.hasRouteGeometry()) {
                        const geom = resolveGeometry(vData);
                        if (geom) v.setRouteGeometry(geom);
                  }
                  v.update(vData);
              }
          }
      } catch (err) {
          console.error("Error fetching focused vehicle", err);
      }
  }

  function handleSelect(id) {
      if (selectedId === id) {
          if (vehicleState.has(selectedId)) {
              vehicleState.get(selectedId).setSelected(false);
          }
          selectedId = null;
          if (focusInterval) clearInterval(focusInterval);
          focusInterval = null;
          console.log("Cleared focus");
          return;
      }
      
      // Update visual for old selection
      if (selectedId && vehicleState.has(selectedId)) {
          vehicleState.get(selectedId).setSelected(false);
      }

      selectedId = id;
      console.log("Focused on vehicle:", id);

      // Update visual for new selection
      if (vehicleState.has(id)) {
          vehicleState.get(id).setSelected(true);
      }
      
      // Immediate fetch
      fetchVehicleUpdate(id);
      
      // High frequency polling for the focused vehicle (1s)
      if (focusInterval) clearInterval(focusInterval);
      focusInterval = setInterval(() => fetchVehicleUpdate(id), 1000); 
  }


  async function stopVehicleStream(type) {
      if (sseConnections[type]) {
          sseConnections[type].close();
          sseConnections[type] = null;
      }
      // Remove vehicles of this type from the map
      vehicleState.forEach((v, id) => {
          if (v.data.type === type) {
              v.remove();
              vehicleState.delete(id);
          }
      });
  }

  async function startVehicleStream(type) {
      if (sseConnections[type]) return; // Already running

      const url = `${API_BASE}/${type}-sse`;
      console.log(`Connecting to ${type} SSE:`, url);

      const evtSource = new EventSource(url);
      sseConnections[type] = evtSource;
      
      evtSource.onopen = () => {
          console.log(`${type} SSE Connection established.`);
      };
      
      evtSource.onmessage = (e) => {
          try {
              const payload = JSON.parse(e.data);
              if (payload.type === 'vehicles') {
                  // Process in chunks to avoid blocking the main thread
                  const dataArr = payload.data || [];
                  const chunks = [];
                  const size = 15;
                  for (let i = 0; i < dataArr.length; i += size) {
                      chunks.push(dataArr.slice(i, i + size));
                  }

                  const processNext = (idx) => {
                      if (idx >= chunks.length) return;
                      
                      chunks[idx].forEach(vData => {
                          // Optimization: If a vehicle is selected, skip 80% of updates for others
                          if (selectedId && vData.id !== selectedId) {
                              if (Math.random() > 0.3) return;
                          }

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
                              v.setOnSelect(handleSelect);
                              vehicleState.set(vData.id, v);
                          }
                      });

                      if (idx + 1 < chunks.length) {
                          requestAnimationFrame(() => processNext(idx + 1));
                      }
                  };

                  processNext(0);
              }
          } catch (err) {
              console.error(`Error parsing ${type} SSE`, err);
          }
      };

      evtSource.onerror = (err) => {
          console.error(`${type} SSE Error:`, err);
      };
  }

  // Handle filter toggles
  $: if (browser && map) {
      if (enabledTypes.metro) startVehicleStream('metro'); else stopVehicleStream('metro');
  }
  $: if (browser && map) {
      if (enabledTypes.tram) startVehicleStream('tram'); else stopVehicleStream('tram');
  }
  $: if (browser && map) {
      if (enabledTypes.bus) startVehicleStream('bus'); else stopVehicleStream('bus');
  }

  onMount(async () => {
    if (!browser) return;

    // 1. Start fetching network data IMMEDIATELY in parallel with map init
    const networkPromise = fetch('/ret_network.json').then(r => {
        if (!r.ok) throw new Error('Network data missing');
        return r.json();
    });

    // 2. Initialize Map (Base tiles load)
    map = await initMap(mapElement);
    
    // Add click-away listener to the map
    map.on('click', (e) => {
        // Only deselect if the click wasn't on a marker element
        // (Since markers are DOM elements on top, we check e.originalEvent)
        if (e.originalEvent && e.originalEvent.target && e.originalEvent.target.closest('.vehicle-marker')) {
             return; 
        }
        if (selectedId) handleSelect(selectedId);
    });

    // 3. Inject Dependencies for Vehicles
    const maplibreModule = await import('maplibre-gl');
    const maplibregl = maplibreModule.default || maplibreModule;
    Vehicle.injectLibrary(maplibregl);

    // 4. Wait for Network Data and Style
    try {
        const retData = await networkPromise;

        // 5. Process Geometries for Snapping (Background CPU)
        const featureGroups = {};
        (retData.features || []).forEach(f => {
            if (f.geometry && f.properties && f.properties.ref) {
                const ref = f.properties.ref;
                if (!featureGroups[ref]) featureGroups[ref] = [];
                
                if (f.geometry.type === 'LineString') {
                    featureGroups[ref].push(f.geometry.coordinates);
                } else if (f.geometry.type === 'MultiLineString') {
                    f.geometry.coordinates.forEach(coords => {
                        featureGroups[ref].push(coords);
                    });
                }
            }
        });
        for (const [ref, coords] of Object.entries(featureGroups)) {
            routeGeometries[ref] = turf.multiLineString(coords);
        }
        console.log(`Loaded ${Object.keys(routeGeometries).length} route geometries for snapping.`);

        // 6. Add Lines to Map
        const addLayers = (data) => {
             if (!map || !map.getStyle()) return;
             if (map.getSource('ret-data')) return;

             if (data) {
                 map.addSource('ret-data', { type: 'geojson', data: data });
               
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
             }
             
             // 7. Start Live Vehicle Stream
             // (Now controlled by reactive checkboxes)
             // startVehicleStream();
        };

        if (map.loaded()) {
            addLayers(retData);
        } else {
            map.once('load', () => addLayers(retData));
        }

    } catch (err) {
        console.error("Failed to load network geometry", err);
        // Fallback: Start vehicles anyway
    }
  });

  onDestroy(() => {
     Object.values(sseConnections).forEach(conn => {
         if (conn) conn.close();
     });
     if (focusInterval) {
         clearInterval(focusInterval);
     }
     vehicleState.forEach(v => v.remove());
     vehicleState.clear();
  });
</script>

<div bind:this={mapElement} class="map-container"></div>

<div class="sidebar">
    <div class="filter-group">
        <label>
            <input type="checkbox" bind:checked={enabledTypes.metro} />
            Metro
        </label>
        <label>
            <input type="checkbox" bind:checked={enabledTypes.tram} />
            Tram
        </label>
        <label>
            <input type="checkbox" bind:checked={enabledTypes.bus} />
            Bus
        </label>
    </div>
</div>

<style>
  .map-container {
    width: 100%;
    height: 100%;
    position: absolute;
    top: 0;
    left: 0;
  }

  .sidebar {
      position: absolute;
      top: 80px;
      right: 20px;
      padding: 15px;
      background: rgba(0, 22, 61, 0.85);
      backdrop-filter: blur(4px);
      color: white;
      border-radius: 8px;
      font-family: sans-serif;
      z-index: 1001;
      box-shadow: 0 4px 15px rgba(0,0,0,0.4);
      min-width: 120px;
  }

  .filter-group {
      display: flex;
      flex-direction: column;
      gap: 10px;
  }

  .filter-group label {
      display: flex;
      align-items: center;
      gap: 8px;
      cursor: pointer;
      font-size: 1rem;
      user-select: none;
  }

  .filter-group input {
      width: 18px;
      height: 18px;
      cursor: pointer;
  }
</style>
