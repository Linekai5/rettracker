<script>
  import { onMount, onDestroy } from 'svelte';
  import { initMap } from './map.js';
  import { browser, dev } from '$app/environment';
  // Removed direct import to avoid bundling 2.4MB JSON
  // import retData from '$lib/assets/ret_network.json';
  import * as turf from '@turf/turf';

  let mapElement;
  let map;
  let sseConnection = null;
  let selectedId = null;
  let focusInterval = null;
  
  const stopDataMap = new Map(); // Store latest stop data from SSE
  let hoverPopup = null;
  let searchQuery = "";
  let searchedStopGeom = { type: "FeatureCollection", features: [] };
  let selectedStop = null;
  let selectedModeFilter = 'all';

  const routeGeometries = {}; // route_id -> MultiLineString
  const resolvedCache = new Map(); // rid + hint -> geometry
  
  // Use relative URLs to leverage Vite proxy in dev (works in Codespaces)
  // In production, fallback to the external backend URL
  const API_BASE = dev ? '' : 'https://rettrack.dfelix.systems'; 

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
          selectedId = null;
          return;
      }
      selectedId = id;
  }

  function handleSearch() {
      if (!searchQuery.trim()) {
          searchedStopGeom = { type: "FeatureCollection", features: [] };
          updateSearchLayer();
          return;
      }
      const query = searchQuery.toLowerCase();
      
      const matchedGroups = new Map();
      
      for (const sData of stopDataMap.values()) {
          if (sData.name && sData.name.toLowerCase().includes(query)) {
              const name = sData.name;
              if (!matchedGroups.has(name)) {
                  matchedGroups.set(name, {
                      name: name,
                      types: new Set(),
                      latSum: 0,
                      lonSum: 0,
                      count: 0,
                      arrivals: []
                  });
              }
              const group = matchedGroups.get(name);
              if (sData.type) group.types.add(sData.type);
              group.latSum += sData.lat;
              group.lonSum += sData.lon;
              group.count++;
              if (sData.passages && Array.isArray(sData.passages)) {
                  group.arrivals.push(...sData.passages);
              }
          }
      }

      const features = [];
      let featureId = 1;
      for (const group of matchedGroups.values()) {
          const avgLat = group.latSum / group.count;
          const avgLon = group.lonSum / group.count;
          const typesArr = Array.from(group.types).map(t => t.charAt(0).toUpperCase() + t.slice(1));
          
          const sortedArrivals = group.arrivals
              .filter(a => a.expected_arrival)
              .sort((a, b) => new Date(a.expected_arrival) - new Date(b.expected_arrival));
          
          features.push({
              type: "Feature",
              id: featureId++,
              geometry: { type: "Point", coordinates: [avgLon, avgLat] },
              properties: {
                  name: group.name || 'Unknown Stop',
                  typeList: JSON.stringify(Array.from(group.types)),
                  type: typesArr.join(', ') || 'Unknown',
                  arrivals: JSON.stringify(sortedArrivals)
              }
          });
      }

      searchedStopGeom = {
          type: "FeatureCollection",
          features: features
      };
      
      updateSearchLayer();

      // Update selectedStop if it exists and matches a group so arrivals update in real-time
      if (selectedStop && matchedGroups.has(selectedStop.name)) {
          const group = matchedGroups.get(selectedStop.name);
          const sortedArrivals = group.arrivals
              .filter(a => a.expected_arrival)
              .sort((a, b) => new Date(a.expected_arrival) - new Date(b.expected_arrival));
          
          selectedStop = {
              ...selectedStop,
              arrivals: sortedArrivals
          };
      }
      
      // Close panel if search changes and removes current stop
      if (!searchQuery.trim() || features.length === 0) {
          closePanel();
      }
  }

  function updateSearchLayer() {
      if (!map || !map.getSource('search-stop-source')) return;
      map.getSource('search-stop-source').setData(searchedStopGeom);
  }

  function closePanel() {
      // stop tracking when closing the panel
      stopTracking();
      selectedStop = null;
      if (map) {
          map.flyTo({
              center: [4.4777, 51.9244],
              zoom: 12,
              duration: 800
          });
      }
  }

  async function stopStopsStream() {
      if (sseConnection) {
          sseConnection.close();
          sseConnection = null;
      }
      stopDataMap.clear();
  }

  async function startStopsStream() {
      if (sseConnection) return; // Already running

      const url = `${API_BASE}/stops-sse`;
      console.log(`Connecting to Stops SSE:`, url);

      const evtSource = new EventSource(url);
      sseConnection = evtSource;
      
      evtSource.onopen = () => {
          console.log(`Stops SSE Connection established.`);
      };
      
      evtSource.onmessage = async (e) => {
          try {
              const payload = JSON.parse(e.data);
              const dataArr = payload.updates || [];
              if (!dataArr.length) return;

              dataArr.forEach(sData => {
                  stopDataMap.set(sData.id, sData);
              });
              
              if (searchQuery.trim()) {
                  handleSearch();
              }
              
              if (selectedStop) {
                  const targetName = selectedStop.name;
                  const allPassages = [];
                  
                  // Aggregate passages from all platforms with the matching name
                  for (const s of stopDataMap.values()) {
                      if (s.name === targetName && s.passages) {
                          allPassages.push(...s.passages);
                      }
                  }
                  
                  // Sort and update if we found new data
                  if (allPassages.length > 0) {
                      allPassages.sort((a, b) => {
                          const timeA = a.expected_arrival ? new Date(a.expected_arrival).getTime() : 0;
                          const timeB = b.expected_arrival ? new Date(b.expected_arrival).getTime() : 0;
                          return timeA - timeB;
                      });
                      
                      // Only update if data actually changed to avoid re-renders? 
                      // actually Svelte handles object identity checks, but we are creating a new object every time.
                      // Let's just update it.
                      selectedStop = { ...selectedStop, arrivals: allPassages };
                  }
              }

          } catch (err) {
              console.error(`Error parsing Stops SSE`, err);
          }
      };

      evtSource.onerror = (err) => {
          console.error(`Stops SSE Error:`, err);
      };
  }

  onMount(async () => {
    if (!browser) return;

    const maplibreModule = await import('maplibre-gl');
    const maplibregl = maplibreModule.default || maplibreModule;
    maplibreglVar = maplibregl;

    // Initialize the shared hover popup
    hoverPopup = new maplibregl.Popup({ offset: 10, closeButton: false, closeOnClick: false });

    // 1. Start fetching network data IMMEDIATELY in parallel with map init
    const networkPromise = fetch('/ret_network.json').then(r => {
        if (!r.ok) throw new Error('Network data missing');
        return r.json();
    });

    // 2. Initialize Map (Base tiles load)
    map = await initMap(mapElement);
    
    // Add click-away listener to the map
    map.on('click', (e) => {
        if (selectedId) handleSelect(selectedId);
        
        // If they click on the map but not on the point, close the panel
        const features = map.queryRenderedFeatures(e.point, { layers: ['search-stop-layer'] });
        if (!features.length && selectedStop) {
            closePanel();
        }
    });

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
                    id: 'ret-bus',
                    type: 'line',
                    source: 'ret-data',
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
                    id: 'ret-tram',
                    type: 'line',
                    source: 'ret-data',
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
                    id: 'ret-metro',
                    type: 'line',
                    source: 'ret-data',
                    filter: ['==', 'layer', 'metro'],
                    layout: { 'line-join': 'round', 'line-cap': 'round' },
                    paint: {
                        'line-color': ['get', 'color'],
                        'line-width': ['interpolate', ['linear'], ['zoom'], 10, 1.5, 14, 3],
                        'line-opacity': 1.0
                    }
                 });

                 // Initialize Search Stop GeoJSON Source & WebGL Layer to prevent zoom jitter
                 map.addSource('search-stop-source', { type: 'geojson', data: searchedStopGeom });
                 
                 map.addLayer({
                    id: 'search-stop-layer',
                    type: 'circle',
                    source: 'search-stop-source',
                    paint: {
                        'circle-color': '#FFF',
                        'circle-radius': ['case', ['boolean', ['feature-state', 'hover'], false], 8, 5],
                        'circle-stroke-width': 1.5,
                        'circle-stroke-color': '#000000'
                    }
                 });

                 let hoveredStateId = null;

                 // Render Tooltip exclusively on hover of the search result
                 map.on('mouseenter', 'search-stop-layer', (e) => {
                     map.getCanvas().style.cursor = 'pointer';
                     const props = e.features[0].properties;

                     if (e.features.length > 0) {
                         if (hoveredStateId !== null) {
                             map.setFeatureState(
                                 { source: 'search-stop-source', id: hoveredStateId },
                                 { hover: false }
                             );
                         }
                         hoveredStateId = e.features[0].id;
                         map.setFeatureState(
                             { source: 'search-stop-source', id: hoveredStateId },
                             { hover: true }
                         );
                     }

                     const html = `
                         <div style="font-family: Arial, sans-serif; padding: 4px; min-width: 100px; text-align: center;">
                             <div style="font-weight: bold; font-size: 14px;">${props.name}</div>
                         </div>
                     `;
                     
                     hoverPopup.setLngLat(e.features[0].geometry.coordinates).setHTML(html).addTo(map);
                 });

                 map.on('mouseleave', 'search-stop-layer', () => {
                     map.getCanvas().style.cursor = '';
                     hoverPopup.remove();
                     
                     if (hoveredStateId !== null) {
                         map.setFeatureState(
                             { source: 'search-stop-source', id: hoveredStateId },
                             { hover: false }
                         );
                     }
                     hoveredStateId = null;
                 });
                 
                 map.on('click', 'search-stop-layer', (e) => {
                     if (!e.features.length) return;
                     const feature = e.features[0];
                     const coords = feature.geometry.coordinates;
                     
                     const props = feature.properties;
                     let arrivals = [];
                     let typeList = [];
                     try {
                         arrivals = JSON.parse(props.arrivals || "[]");
                         typeList = JSON.parse(props.typeList || "[]");
                     } catch(err) {}
                     
                     // stop any previous vehicle tracking when selecting a new stop
                     stopTracking();
                     
                     selectedStop = {
                         name: props.name,
                         types: typeList.map(t => t.charAt(0).toUpperCase() + t.slice(1)),
                         arrivals: arrivals
                     };
                     selectedModeFilter = 'all';
                     
                     // Fly to stop
                     map.flyTo({
                         center: coords,
                         zoom: 16,
                         duration: 800
                     });
                     
                     hoverPopup.remove();
                 });
             }
             
             // 7. Start Live Stops Stream
             startStopsStream();
        };

        if (map.loaded()) {
            addLayers(retData);
        } else {
            map.once('load', () => addLayers(retData));
        }

    } catch (err) {
        console.error("Failed to load network geometry", err);
        // Fallback: Start stops anyway
        startStopsStream();
    }
  });

  onDestroy(() => {
     if (sseConnection) sseConnection.close();
     if (hoverPopup) hoverPopup.remove();
     stopDataMap.clear();
  });

  let maplibreglVar = null;
  let trackedVehicleId = null;
  let trackingInterval = null;
  let trackingAnimationFrame = null;
  let trackingCurrentPos = null; // [lon, lat]
  let trackingRouteGeom = null; // turf LineString/MultiLineString
  let trackingRouteLengthKm = 0;
  let trackingCurrentDistKm = null; // distance along line in km
  let trackingPopup = null;
  let trackingMeta = null; // { line, destination, lastSeen, nextStops: [{name, expected_arrival}], ... }
  const TRACK_POLL_MS = 1500; // how often to fetch single-vehicle updates
  const TRACK_ANIM_MS = 1000; // interpolation duration
  const TRACK_SOURCE_ID = 'tracking-vehicle-source';
  const TRACK_LAYER_ID = 'tracking-vehicle-layer';
  const JITTER_THRESHOLD_METERS = 5; // small movement below this is considered stationary

  let trackingSSE = null;

  function stopTracking() {
      if (trackingSSE) {
          trackingSSE.close();
          trackingSSE = null;
      }
      if (trackingInterval) {
          clearInterval(trackingInterval);
          trackingInterval = null;
      }
      if (trackingAnimationFrame) {
          cancelAnimationFrame(trackingAnimationFrame);
          trackingAnimationFrame = null;
      }
      if (trackingPopup) {
          trackingPopup.remove();
          trackingPopup = null;
      }
      trackedVehicleId = null;
      trackingCurrentPos = null;
      if (map) {
          try {
              if (map.getLayer(TRACK_LAYER_ID)) map.removeLayer(TRACK_LAYER_ID);
          } catch(e) {}
          try {
              if (map.getSource(TRACK_SOURCE_ID)) map.removeSource(TRACK_SOURCE_ID);
          } catch(e) {}
      }
  }

  async function fetchVehicleOnce(id) {
      if (!id) return null;
      try {
          const res = await fetch(`${API_BASE}/vehicles/${id}`);
          if (!res.ok) return null;
          const v = await res.json();
          if (!v || !v.lat || !v.lon) return null;
          return v;
      } catch (e) {
          console.error('Error fetching vehicle', e);
          return null;
      }
  }

  function lerp(a, b, t) {
      return a + (b - a) * t;
  }

  function animateMarker(from, to, duration = TRACK_ANIM_MS) {
      const start = performance.now();
      const animate = (now) => {
          const t = Math.min(1, (now - start) / duration);
          const curLon = lerp(from[0], to[0], t);
          const curLat = lerp(from[1], to[1], t);
          if (map && map.getSource(TRACK_SOURCE_ID)) {
              map.getSource(TRACK_SOURCE_ID).setData({ type: 'FeatureCollection', features: [{ type: 'Feature', geometry: { type: 'Point', coordinates: [curLon, curLat] } }] });
          }
          if (t < 1) {
              trackingAnimationFrame = requestAnimationFrame(animate);
          } else {
              trackingAnimationFrame = null;
              trackingCurrentPos = to.slice();
          }
      };
      if (trackingAnimationFrame) cancelAnimationFrame(trackingAnimationFrame);
      trackingAnimationFrame = requestAnimationFrame(animate);
  }

  // New helper: animate along route between two distances along a line
  function animateAlongRouteDistances(routeGeom, fromDist, toDist, duration = TRACK_ANIM_MS) {
      if (!routeGeom || fromDist === null || toDist === null) return;
      
      const start = performance.now();
      const step = (now) => {
          const t = Math.min(1, (now - start) / duration);
          const currentDist = lerp(fromDist, toDist, t);
          try {
              const pt = turf.along(routeGeom, currentDist, { units: 'kilometers' });
              const coords = pt.geometry.coordinates;
              if (map && map.getSource(TRACK_SOURCE_ID)) {
                  map.getSource(TRACK_SOURCE_ID).setData({ 
                      type: 'FeatureCollection', 
                      features: [{ type: 'Feature', geometry: { type: 'Point', coordinates: coords } }] 
                  });
              }
              trackingCurrentPos = coords.slice();
              
              // update popup position
              if (trackingPopup) {
                  trackingPopup.setLngLat(trackingCurrentPos);
              }
          } catch (e) {
              console.error("Error in animateAlongRouteDistances step", e);
          }

          if (t < 1) {
              trackingAnimationFrame = requestAnimationFrame(step);
          } else {
              trackingAnimationFrame = null;
          }
      };
      if (trackingAnimationFrame) cancelAnimationFrame(trackingAnimationFrame);
      trackingAnimationFrame = requestAnimationFrame(step);
  }

  // New helper: animate along route between two coordinates (snapped to route)
  function animateAlongCoords(routeGeom, fromCoord, toCoord, duration = TRACK_ANIM_MS) {
      // Ensure from/to are [lon, lat]
      if (!fromCoord || !toCoord) {
          if (toCoord && map && map.getSource(TRACK_SOURCE_ID)) {
              map.getSource(TRACK_SOURCE_ID).setData({ type: 'FeatureCollection', features: [{ type: 'Feature', geometry: { type: 'Point', coordinates: toCoord } }] });
              trackingCurrentPos = toCoord.slice();
          }
          return;
      }
      try {
          const fromPt = turf.point(fromCoord);
          const toPt = turf.point(toCoord);
          const slice = turf.lineSlice(fromPt, toPt, routeGeom);
          const sliceLenKm = turf.length(slice, { units: 'kilometers' });

          if (sliceLenKm === 0) {
              if (map && map.getSource(TRACK_SOURCE_ID)) map.getSource(TRACK_SOURCE_ID).setData({ type: 'FeatureCollection', features: [{ type: 'Feature', geometry: { type: 'Point', coordinates: toCoord } }] });
              trackingCurrentPos = toCoord.slice();
              return;
          }

          const start = performance.now();
          const step = (now) => {
              const t = Math.min(1, (now - start) / duration);
              const distKm = t * sliceLenKm;
              const pt = turf.along(slice, distKm, { units: 'kilometers' });
              const coords = pt.geometry.coordinates;
              if (map && map.getSource(TRACK_SOURCE_ID)) {
                  map.getSource(TRACK_SOURCE_ID).setData({ type: 'FeatureCollection', features: [{ type: 'Feature', geometry: { type: 'Point', coordinates: coords } }] });
              }
              trackingCurrentPos = coords.slice();
              if (t < 1) trackingAnimationFrame = requestAnimationFrame(step);
              else trackingAnimationFrame = null;
          };
          if (trackingAnimationFrame) cancelAnimationFrame(trackingAnimationFrame);
          trackingAnimationFrame = requestAnimationFrame(step);
      } catch (e) {
          // fallback to linear animation
          animateMarker(fromCoord, toCoord, duration);
      }
  }

  async function startTracking(vehicleId) {
      if (!vehicleId) return;
      // If same vehicle already tracked, do nothing
      if (trackedVehicleId === vehicleId) return;

      // Stop any existing tracking
      stopTracking();

      trackedVehicleId = vehicleId;

      // Ensure map source/layer exist
      if (map && !map.getSource(TRACK_SOURCE_ID)) {
          map.addSource(TRACK_SOURCE_ID, { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
          map.addLayer({
              id: TRACK_LAYER_ID,
              type: 'circle',
              source: TRACK_SOURCE_ID,
              paint: {
                  'circle-radius': 8,
                  'circle-color': '#FFD500',
                  'circle-stroke-width': 2,
                  'circle-stroke-color': '#000'
              }
          });
      }

      // create popup if possible
      try {
          if (map && maplibreglVar) {
              trackingPopup = new maplibreglVar.Popup({ offset: 10, closeButton: false, closeOnClick: false });
          }
      } catch(e) { trackingPopup = null; }

      // Connect to the high-frequency single-vehicle SSE endpoint
      const url = `${API_BASE}/vehicles/${vehicleId}/live`;
      console.log(`Tracking high-frequency SSE:`, url);
      const evtSource = new EventSource(url);
      trackingSSE = evtSource;

      evtSource.onmessage = async (e) => {
          try {
              const nv = JSON.parse(e.data);
              if (!nv || !nv.lat || !nv.lon) return;

              // Update metadata immediately (arrival predictions still come from global stopDataMap)
              updateTrackingMeta(nv);

              const newPos = [nv.lon, nv.lat];

              // Handle first position
              if (!trackingCurrentPos) {
                  trackingCurrentPos = newPos.slice();
                  // resolve route geometry for snapping
                  try {
                      trackingRouteGeom = resolveGeometry(nv);
                      if (trackingRouteGeom) {
                          trackingRouteLengthKm = turf.length(trackingRouteGeom, { units: 'kilometers' });
                          const snapped = turf.nearestPointOnLine(trackingRouteGeom, trackingCurrentPos);
                          trackingCurrentDistKm = snapped && snapped.properties ? snapped.properties.location : 0;
                          const snappedPt = turf.along(trackingRouteGeom, trackingCurrentDistKm, { units: 'kilometers' });
                          trackingCurrentPos = snappedPt.geometry.coordinates.slice();
                          
                          if (map) {
                                map.flyTo({ center: trackingCurrentPos, zoom: 15, duration: 1000 });
                          }
                      }
                  } catch(e) { 
                      console.error("Error resolving geometry", e);
                      trackingRouteGeom = null; 
                      trackingRouteLengthKm = 0; 
                  }
                  
                  if (map && map.getSource(TRACK_SOURCE_ID)) {
                      map.getSource(TRACK_SOURCE_ID).setData({ type: 'FeatureCollection', features: [{ type: 'Feature', geometry: { type: 'Point', coordinates: trackingCurrentPos } }] });
                  }
                  
                  if (trackingPopup) {
                      trackingPopup.setLngLat(trackingCurrentPos).setHTML(renderTrackingPopup()).addTo(map);
                  }
                  return;
              }

              if (trackingRouteGeom) {
                  // compute distance along line for new positions
                  let newDistKm = 0;
                  try { 
                      const newSnapped = turf.nearestPointOnLine(trackingRouteGeom, newPos); 
                      newDistKm = newSnapped && newSnapped.properties ? newSnapped.properties.location : 0;
                  } catch(e) { 
                      console.error("Error snapping new point", e);
                      newDistKm = trackingCurrentDistKm; 
                  }

                  const movedMeters = Math.abs(newDistKm - (trackingCurrentDistKm || 0)) * 1000;
                  if (movedMeters > JITTER_THRESHOLD_METERS) {
                      // animate along route
                      const fromDist = trackingCurrentDistKm || 0;
                      const toDist = newDistKm;
                      // Use a duration slightly less than the update frequency (1s) for smooth transitions
                      animateAlongRouteDistances(trackingRouteGeom, fromDist, toDist, 1000);
                      trackingCurrentDistKm = newDistKm;
                  }
              } else {
                  // no route geom, fallback to simple animation
                  animateMarker(trackingCurrentPos.slice(), newPos.slice(), 1000);
              }

              // update popup content
              if (trackingPopup) {
                  trackingPopup.setHTML(renderTrackingPopup());
              }
          } catch(err) {
              console.error(`Error parsing Tracking SSE`, err);
          }
      };

      evtSource.onerror = (err) => {
          console.error(`Tracking SSE Error:`, err);
      };
  }

  function updateTrackingMeta(v) {
      if (!v) return;
      // v is vehicle data { id, lat, lon, line, destination, timestamp }
      const now = new Date();
      const lastSeen = v.timestamp || now.toISOString();
      // Gather next stops from stopDataMap where journey_id matches
      const nextStops = [];
      const vehicleId = (v.entity_id || v.id || "").toString();

      for (const s of stopDataMap.values()) {
          if (s.passages && Array.isArray(s.passages)) {
              for (const p of s.passages) {
                  const passageJourneyId = (p.journey_id || "").toString();
                  if (passageJourneyId === vehicleId) {
                      nextStops.push({ 
                          name: s.name || p.timing_point || 'Unknown', 
                          expected_arrival: p.expected_arrival 
                      });
                  }
              }
          }
      }
      // Sort and deduplicate based on arrival time (same vehicle might visit multiple platforms at one stop)
      nextStops.sort((a, b) => new Date(a.expected_arrival) - new Date(b.expected_arrival));
      
      const uniqueNextStops = [];
      const seenTimes = new Set();
      for (const ns of nextStops) {
          const t = new Date(ns.expected_arrival).getTime();
          if (!seenTimes.has(t)) {
              uniqueNextStops.push(ns);
              seenTimes.add(t);
          }
      }

      trackingMeta = {
          line: v.line,
          destination: v.destination,
          lastSeen: lastSeen,
          nextStops: uniqueNextStops.slice(0, 5)
      };
  }

  function renderTrackingPopup() {
      if (!trackingMeta) return `<div style="font-size:12px;padding:6px">Tracking...</div>`;
      const lines = [`<div style="font-weight:bold">${trackingMeta.line || '?'} → ${trackingMeta.destination || 'Unknown'}</div>`];
      lines.push(`<div style="font-size:11px;color:#666">Last: ${new Date(trackingMeta.lastSeen).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'})}</div>`);
      if (trackingMeta.nextStops && trackingMeta.nextStops.length) {
          lines.push('<div style="margin-top:6px;font-size:12px">Next stops:</div>');
          lines.push('<ul style="margin:4px 0 0 14px;padding:0;font-size:12px;color:#333">');
          for (const s of trackingMeta.nextStops) {
              lines.push(`<li>${s.name} — ${new Date(s.expected_arrival).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}</li>`);
          }
          lines.push('</ul>');
      }
      return lines.join('');
  }

</script>

<div class="search-container">
    <input type="text" placeholder="Search stop..." bind:value={searchQuery} on:input={handleSearch} class="search-input" />
</div>

{#if selectedStop}
<div class="side-panel">
    <div class="panel-header">
        <h2>{selectedStop.name}</h2>
        <button on:click={closePanel} class="close-btn">X</button>
    </div>
    
    {#if selectedStop.types.length > 1}
    <div class="mode-filters">
        <button class:active={selectedModeFilter === 'all'} on:click={() => selectedModeFilter = 'all'}>All</button>
        {#each selectedStop.types as mode}
            <button class:active={selectedModeFilter === mode.toLowerCase()} on:click={() => selectedModeFilter = mode.toLowerCase()}>{mode}</button>
        {/each}
    </div>
    {/if}
    
    <div class="arrivals-list">
        {#if trackedVehicleId && trackingMeta}
            <div class="tracking-panel">
                <div style="font-weight:bold; margin-bottom:6px">{trackingMeta.line || '?'} → {trackingMeta.destination || 'Unknown'}</div>
                <div style="font-size:12px; color:#666">Last seen: {new Date(trackingMeta.lastSeen).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'})}</div>
                {#if trackingMeta.nextStops && trackingMeta.nextStops.length}
                    <div style="margin-top:8px; font-weight:600">Upcoming stops</div>
                    <ul style="margin:6px 0 0 14px; padding:0; font-size:13px; color:#333">
                        {#each trackingMeta.nextStops as ns}
                            <li style="margin-bottom:4px">{ns.name} — {new Date(ns.expected_arrival).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}</li>
                        {/each}
                    </ul>
                {:else}
                    <div style="padding:10px; color:#666">No stop predictions available.</div>
                {/if}
            </div>
        {:else}
            {#each selectedStop.arrivals.filter(a => selectedModeFilter === 'all' || (a.type || '').toLowerCase() === selectedModeFilter || selectedStop.types.length === 1) as arr}
                <div class="arrival-item" on:click={() => startTracking(arr.journey_id)} class:tracking={trackedVehicleId === arr.journey_id}>
                    <div class="arrival-line">{arr.line || '?'}</div>
                    <div class="arrival-dest">{arr.destination || 'Unknown'}</div>
                    <div class="arrival-time">{new Date(arr.expected_arrival).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})}</div>
                </div>
            {/each}
            {#if selectedStop.arrivals.filter(a => selectedModeFilter === 'all' || (a.type || '').toLowerCase() === selectedModeFilter || selectedStop.types.length === 1).length === 0}
                <div style="padding: 10px; color: #666;">No upcoming arrivals.</div>
            {/if}
        {/if}
    </div>
</div>
{/if}

<div bind:this={mapElement} class="map-wrapper"></div>

<style>
  .map-wrapper {
    width: 100%;
    height: 100%;
    position: absolute;
    top: 0;
    left: 0;
    z-index: 1; /* Below the search container */
  }

  .search-container {
    position: absolute;
    top: 80px; /* 60px header + 20px padding */
    right: 20px;
    z-index: 10;
  }

  .search-input {
    padding: 10px 15px;
    font-size: 16px;
    border: 2px solid #ccc;
    border-radius: 8px;
    width: 250px;
    box-shadow: 0 4px 6px rgba(0,0,0,0.1);
    outline: none;
    transition: border-color 0.2s;
  }

  .search-input:focus {
    border-color: #00163d;
  }
  
  .side-panel {
    position: absolute;
    top: 80px;
    right: 20px;
    width: 320px;
    background: white;
    border-radius: 8px;
    box-shadow: 0 4px 12px rgba(0,0,0,0.15);
    z-index: 15;
    display: flex;
    flex-direction: column;
    max-height: calc(100vh - 100px);
    overflow: hidden;
  }
  
  .panel-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 15px;
    border-bottom: 1px solid #eee;
  }
  
  .panel-header h2 {
    margin: 0;
    font-size: 18px;
  }
  
  .close-btn {
    background: none;
    border: none;
    font-size: 16px;
    cursor: pointer;
    font-weight: bold;
    color: #666;
  }
  
  .mode-filters {
    display: flex;
    gap: 8px;
    padding: 10px 15px;
    background: #f9f9f9;
    border-bottom: 1px solid #eee;
    flex-wrap: wrap;
  }
  
  .mode-filters button {
    border: 1px solid #ddd;
    background: white;
    border-radius: 12px;
    padding: 4px 12px;
    font-size: 13px;
    cursor: pointer;
  }
  
  .mode-filters button.active {
    background: #00163d;
    color: white;
    border-color: #00163d;
  }
  
  .arrivals-list {
    flex-grow: 1;
    overflow-y: auto;
    padding: 10px 15px;
  }
  
  .arrival-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 12px;
    border-bottom: 1px solid #f1f1f1;
    cursor: pointer;
  }

  .arrival-item:hover {
    background: #fafafa;
  }

  .arrival-item.tracking {
    background: linear-gradient(90deg, rgba(255,213,0,0.12), rgba(255,213,0,0.06));
    border-left: 4px solid #FFD500;
  }
  
  .arrival-line {
    font-weight: bold;
    min-width: 30px;
  }
  
  .arrival-dest {
    flex-grow: 1;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    padding-right: 10px;
  }
  
  .arrival-time {
    font-family: monospace;
    font-size: 14px;
    color: #333;
  }

  .tracking-panel {
    padding: 10px 15px;
    background: #f0f8ff;
    border-radius: 8px;
    margin-top: 10px;
  }
</style>