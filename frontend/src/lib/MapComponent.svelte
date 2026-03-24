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
              if (sData.arrivals && Array.isArray(sData.arrivals)) {
                  group.arrivals.push(...sData.arrivals);
              }
          }
      }

      const features = [];
      for (const group of matchedGroups.values()) {
          const avgLat = group.latSum / group.count;
          const avgLon = group.lonSum / group.count;
          const typesArr = Array.from(group.types).map(t => t.charAt(0).toUpperCase() + t.slice(1));
          
          const sortedArrivals = group.arrivals
              .filter(a => a.ExpectedDepartureTime)
              .sort((a, b) => new Date(a.ExpectedDepartureTime) - new Date(b.ExpectedDepartureTime))
              .slice(0, 5);
          
          features.push({
              type: "Feature",
              geometry: { type: "Point", coordinates: [avgLon, avgLat] },
              properties: {
                  name: group.name || 'Unknown Stop',
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
  }

  function updateSearchLayer() {
      if (!map || !map.getSource('search-stop-source')) return;
      map.getSource('search-stop-source').setData(searchedStopGeom);
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
              
              // Re-run search in case the searched stop was just added/updated
              if (searchQuery.trim()) {
                  handleSearch();
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
                        'circle-color': '#FFD500',
                        'circle-radius': 5,
                        'circle-stroke-width': 1.5,
                        'circle-stroke-color': '#000000'
                    }
                 });

                 // Render Tooltip exclusively on hover of the search result
                 map.on('mouseenter', 'search-stop-layer', (e) => {
                     map.getCanvas().style.cursor = 'pointer';
                     const props = e.features[0].properties;

                     const typeStr = props.type || "Unknown";
                     let arrivalsHtml = "";
                     try {
                         const arrivals = JSON.parse(props.arrivals || "[]");
                         if (arrivals.length > 0) {
                             arrivalsHtml = `<div style="margin-top: 6px; border-top: 1px solid #ddd; padding-top: 6px;">`;
                             arrivalsHtml += `<div style="font-size: 11px; font-weight: bold; margin-bottom: 4px; color: #333;">Next Arrivals:</div>`;
                             for (const arr of arrivals) {
                                 const time = new Date(arr.ExpectedDepartureTime).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
                                 const dest = arr.DestinationName50 || 'Unknown';
                                 const line = arr.LinePublicNumber || '?';
                                 arrivalsHtml += `
                                 <div style="font-size: 11px; display: flex; justify-content: space-between; margin-bottom: 2px;">
                                     <span style="font-weight:bold; margin-right: 6px; min-width: 20px;">${line}</span>
                                     <span style="flex-grow: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 120px; margin-right: 8px;" title="${dest}">${dest}</span>
                                     <span>${time}</span>
                                 </div>`;
                             }
                             arrivalsHtml += `</div>`;
                         }
                     } catch(err) {
                         console.error("Error parsing arrivals for popup", err);
                     }

                     const html = `
                         <div style="font-family: Arial, sans-serif; padding: 4px; min-width: 180px;">
                             <div style="font-weight: bold; font-size: 14px; margin-bottom: 2px;">${props.name}</div>
                             <div style="font-size: 12px; color: #666;">${typeStr}</div>
                             ${arrivalsHtml}
                         </div>
                     `;
                     
                     hoverPopup.setLngLat(e.features[0].geometry.coordinates).setHTML(html).addTo(map);
                 });

                 map.on('mouseleave', 'search-stop-layer', () => {
                     map.getCanvas().style.cursor = '';
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
</script>

<div class="search-container">
    <input type="text" placeholder="Search stop..." bind:value={searchQuery} on:input={handleSearch} class="search-input" />
</div>

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
</style>