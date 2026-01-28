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
  let sseConnection = null;
  let selectedId = null;
  let focusInterval = null;
  
  const vehicleState = new Map(); // Map<id, Vehicle>
  const routeGeometries = {}; // route_id -> MultiLineString
  const resolvedCache = new Map(); // rid + hint -> geometry
  
  // Use relative URLs to leverage Vite proxy in dev (works in Codespaces)
  // In production, this allows serving frontend/backend from same origin or via reverse proxy
  const API_BASE = ''; 

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

      let foundEntry = null;
      let minDist = Infinity;
      let bestRef = null;
      const vPt = [vData.lon, vData.lat];
      
      // Helper: Check type compatibility (e.g. don't snap Bus to Tram line)
      const isCompat = (entry) => {
          // Strict Validation: If types are defined, they MUST match.
          // If the track has no type, we assume it's generic (unlikely for colored tracks).
          // If vehicle has no type, we can't be strict, but we shouldn't snap to specific tracks like Metro.
          if (entry.type && vData.type) {
              return entry.type === vData.type;
          }
          // If strictly one is missing, prevent mixing Metro/Tram/Bus accidentally
          // Assuming all entries have types (bus, tram, metro)
          if (entry.type) return false; 
          return true;
      };
      
      // -- Step 1: Exact Matches (Most Accurate) --
      if (hint && routeGeometries[hint] && isCompat(routeGeometries[hint])) {
          foundEntry = routeGeometries[hint];
      } else if (rid && routeGeometries[rid] && isCompat(routeGeometries[rid])) {
          foundEntry = routeGeometries[rid];
      } else if (METRO_MAPPING[hint] && routeGeometries[METRO_MAPPING[hint]]) {
          foundEntry = routeGeometries[METRO_MAPPING[hint]];
      }
      
      // -- Step 2: Spatial Match (Search within names first) --
      if (!foundEntry && hint) {
          for (const [ref, entry] of Object.entries(routeGeometries)) {
              if (!isCompat(entry)) continue;

              if (ref.includes(hint) || hint.includes(ref)) {
                  try {
                      // Access .geom because routeGeometries stores { geom, type }
                      const snapped = turf.nearestPointOnLine(entry.geom, vPt);
                      if (snapped && snapped.properties.dist < minDist) {
                          minDist = snapped.properties.dist;
                          bestRef = ref;
                      }
                  } catch(e) {}
              }
          }
          if (bestRef && minDist < 0.5) foundEntry = routeGeometries[bestRef];
      }
      
      // -- Step 3: Global Spatial Search (Aggressive Clipping) --
      if (!foundEntry) {
          minDist = Infinity;
          bestRef = null;
          for (const [ref, entry] of Object.entries(routeGeometries)) {
              if (!isCompat(entry)) continue;
              
              try {
                  const snapped = turf.nearestPointOnLine(entry.geom, vPt);
                  if (snapped && snapped.properties.dist < minDist) {
                      minDist = snapped.properties.dist;
                      bestRef = ref;
                  }
              } catch(e) {}
          }
          // Threshold: 0.5 km - stricter backup to avoid snapping to wrong parallel lines
          if (bestRef && minDist < 0.5) {
              foundEntry = routeGeometries[bestRef];
          }
      }
      
      if (foundEntry) {
        resolvedCache.set(cacheKey, foundEntry.geom);
        return foundEntry.geom;
      }
      return null;
  }

  async function fetchVehicleUpdate(id) {
      if (!id) return;
      try {
          const response = await fetch(`/vehicles/${id}`);
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
          // Deselect current
          if (vehicleState.has(selectedId)) {
              vehicleState.get(selectedId).setSelected(false);
          }
          selectedId = null;
          if (focusInterval) {
             clearInterval(focusInterval);
             focusInterval = null;
          }
          return;
      }
      
      // Update visual for old selection
      if (selectedId && vehicleState.has(selectedId)) {
          vehicleState.get(selectedId).setSelected(false);
      }

      selectedId = id;
      
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


async function stopVehicleStream() {
      if (sseConnection) {
          sseConnection.close();
          sseConnection = null;
      }
      // Remove all vehicles
      vehicleState.forEach((v) => v.remove());
      vehicleState.clear();
  }

  async function startVehicleStream() {
      if (sseConnection) return; // Already running

      const url = `/vehicles-sse`;
      console.log(`Connecting to Unified Vehicle SSE:`, url);

      const evtSource = new EventSource(url);
      sseConnection = evtSource;
      
      evtSource.onopen = () => {
          console.log(`Vehicle SSE Connection established.`);
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
              console.error(`Error parsing Vehicle SSE`, err);
          }
      };

      evtSource.onerror = (err) => {
          console.error(`Vehicle SSE Error:`, err);
      };
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
        // Store geometries with their "Type" (Tram, Bus, Metro) to enable strict type-based snapping
        const featureGroups = {}; // ref -> { coords: [], type: 'bus'|'tram'|'metro' }
        
        (retData.features || []).forEach(f => {
            if (f.geometry && f.properties && f.properties.ref) {
                const ref = f.properties.ref;
                const layer = f.properties.layer || 'bus'; // Default to bus if unspecified
                
                if (!featureGroups[ref]) featureGroups[ref] = { coords: [], type: layer };
                
                if (f.geometry.type === 'LineString') {
                    featureGroups[ref].coords.push(f.geometry.coordinates);
                } else if (f.geometry.type === 'MultiLineString') {
                    f.geometry.coordinates.forEach(coords => {
                        featureGroups[ref].coords.push(coords);
                    });
                }
            }
        });
        
        // Convert to Turf Geometries with attached metadata about Type
        for (const [ref, data] of Object.entries(featureGroups)) {
            // routeGeometries[ref] is now an object { geom, type }
            routeGeometries[ref] = {
                geom: turf.multiLineString(data.coords),
                type: data.type.toLowerCase() // 'bus', 'tram', 'metro'
            };
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
             // Unified stream starts automatically
             startVehicleStream();
        };

        if (map.loaded()) {
            addLayers(retData);
        } else {
            map.once('load', () => addLayers(retData));
        }

    } catch (err) {
        console.error("Failed to load network geometry", err);
        // Fallback: Start vehicles anyway
        startVehicleStream();
    }
  });

  onDestroy(() => {
     if (sseConnection) sseConnection.close();
     if (focusInterval) {
         clearInterval(focusInterval);
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