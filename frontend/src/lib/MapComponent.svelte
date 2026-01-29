<script>
  import { onMount, onDestroy } from 'svelte';
  import maplibregl from 'maplibre-gl';
  import 'maplibre-gl/dist/maplibre-gl.css';

  let mapContainer;
  let map;
  let eventSource;
  
  // Direct Map storage for performance (bypass Svelte reactivity system)
  const vehicleMarkers = new Map(); 

  // RET Official-ish Colors
  const COLORS = {
    metro: '#00AFDB', // RET Blue/Cyan
    tram: '#009E4D',  // RET Green (Alternative: Red #E30613)
    bus: '#6E6E6E',   // Dark Gray
    ferry: '#f39c12',
    default: '#333333'
  };

  // Helper to extract clean line numbers (e.g. from "RET:SUB:M-E" -> "E")
  function parseLine(routeId) {
    if (!routeId) return '?';
    const parts = routeId.split(':');
    let line = parts[parts.length - 1]; 
    if (line.startsWith('M-')) line = line.substring(2); // Fix M-E -> E
    return line;
  }

  onMount(() => {
    map = new maplibregl.Map({
      container: mapContainer,
      // Positron is lightweight and high-performance
      style: 'https://basemaps.cartocdn.com/gl/positron-gl-style/style.json',
      center: [4.4777, 51.9244], // Rotterdam Centraal
      zoom: 12,
      pitch: 0,
      bearing: 0,
      antialias: true
    });

    map.on('load', () => {
      connectSSE();
    });
  });

  onDestroy(() => {
    if (eventSource) eventSource.close();
    if (map) map.remove();
  });

  function connectSSE() {
    // Connect to the optimized backend endpoint
    // Fallback to localhost:8000 for local dev if needed, or relative path
    const url = 'http://localhost:8000/vehicles-sse'; 
    eventSource = new EventSource(url);

    eventSource.onmessage = (event) => {
      if (!event.data || event.data.trim() === '') return;
      
      try {
        const payload = JSON.parse(event.data);
        
        // Handle "data" wrapper from new backend structure
        let vehicles = [];
        if (payload.type === 'vehicles' && payload.data) {
          vehicles = payload.data;
        } else if (payload.updates) {
          vehicles = payload.updates; // Fallback for older backend version
        } else if (Array.isArray(payload)) {
          vehicles = payload;
        }

        if (vehicles.length > 0) {
           // Batch visual updates to the next animation frame
           requestAnimationFrame(() => updateVehicles(vehicles));
        }

      } catch (e) {
        console.error('SSE Parse Error:', e);
      }
    };

    eventSource.onerror = () => {
      console.warn('SSE Disconnected. Retrying in 3s...');
      eventSource.close();
      setTimeout(connectSSE, 3000);
    };
  }

  function updateVehicles(vehicles) {
    vehicles.forEach(v => {
      // Handle both old "id" and new keys if needed, preferring standard keys
      const id = v.id || v.entity_id;
      if (!id || !v.lat || !v.lon) return; // Skip invalid vehicles

      if (vehicleMarkers.has(id)) {
        // --- FAST PATH: UPDATE EXISTING ---
        const markerData = vehicleMarkers.get(id);
        const marker = markerData.markerInstance;
        
        // Move marker (MapLibre handles the interpolation internally if we use flyTo, 
        // but setLngLat is instant. We use CSS transition on the element for smoothness)
        marker.setLngLat([v.lon, v.lat]);
        
        // Update rotation (bearing) efficiently via direct DOM access
        if (markerData.arrowElement && v.bearing !== undefined) {
            markerData.arrowElement.style.transform = `rotate(${v.bearing}deg)`;
        }

        // Update popup text only if currently open
        if (marker.getPopup().isOpen()) {
            marker.getPopup().setHTML(generatePopupContent(v));
        }
        
      } else {
        // --- SLOW PATH: CREATE NEW ---
        createMarker(v);
      }
    });

    // Optional: Pruning logic could go here (remove markers not seen in X seconds)
  }

  function createMarker(v) {
    const type = (v.type || 'bus').toLowerCase();
    const routeId = v.route_id || v.line || '?';
    const line = parseLine(routeId);
    const color = COLORS[type] || COLORS.default;

    // Create DOM element container
    const el = document.createElement('div');
    el.className = `v-marker v-${type}`;
    el.style.setProperty('--v-color', color);
    
    // Inner HTML: Arrow for direction, Badge for line number
    el.innerHTML = `
      <div class="v-arrow"></div>
      <div class="v-badge">${line}</div>
    `;
    
    // Cache the arrow element for fast rotation updates later
    const arrowEl = el.querySelector('.v-arrow');
    arrowEl.style.transform = `rotate(${v.bearing || 0}deg)`;

    // Create Popup
    const popup = new maplibregl.Popup({ offset: 25, closeButton: false })
        .setHTML(generatePopupContent(v));

    // Create MapLibre Marker
    const markerInstance = new maplibregl.Marker({ element: el })
      .setLngLat([v.lon, v.lat])
      .setPopup(popup)
      .addTo(map);

    vehicleMarkers.set(v.id || v.entity_id, {
      markerInstance,
      arrowElement: arrowEl,
      type
    });
  }

  function generatePopupContent(v) {
    const routeId = v.route_id || v.line || '?';
    const line = parseLine(routeId);
    const typeUpper = (v.type || 'BUS').toUpperCase();
    
    // Handle headsign from new backend, fallback to destination
    const headsign = v.headsign || v.destination || 'Unknown'; 
    const speed = v.speed ? Math.round(v.speed) : 0;
    
    return `
      <div class="p-container">
        <div class="p-header" style="background: ${COLORS[v.type ? v.type.toLowerCase() : 'default'] || COLORS.default}">
            <span class="p-line-badge">${line}</span> ${typeUpper}
        </div>
        <div class="p-body">
            <div class="p-row"><span>To:</span> <strong>${headsign}</strong></div>
            <div class="p-row"><span>Speed:</span> ${speed} km/h</div>
            <div class="p-row"><span>Delay:</span> ${v.delay || 0}s</div>
        </div>
      </div>
    `;
  }
</script>

<div class="map-wrap" bind:this={mapContainer}></div>

<style>
  .map-wrap {
    width: 100%;
    height: 100vh;
    background: #eef;
  }

  /* --- MARKER STYLES --- */
  :global(.v-marker) {
    width: 32px;
    height: 32px;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    /* Clean, logical z-index handling */
    z-index: 10;
    /* GPU Accelerated movement */
    will-change: transform;
    /* Smooth transition for position updates */
    transition: transform 0.5s linear; 
  }

  /* Higher z-index for Metro/Tram so they aren't covered by buses */
  :global(.v-marker.v-metro), :global(.v-marker.v-tram) {
    z-index: 20;
  }

  /* The Directional Arrow */
  :global(.v-arrow) {
    width: 0; 
    height: 0; 
    border-left: 9px solid transparent;
    border-right: 9px solid transparent;
    border-bottom: 22px solid var(--v-color);
    position: absolute;
    top: 2px;
    /* Pivot around the center of the visual marker */
    transform-origin: 50% 60%; 
    filter: drop-shadow(0px 2px 2px rgba(0,0,0,0.3));
    transition: transform 0.3s ease-out; /* Smooth rotation */
  }

  /* The Line Number Badge */
  :global(.v-badge) {
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    background: #fff;
    color: #222;
    font-size: 11px;
    font-weight: 800;
    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
    border: 2px solid var(--v-color);
    border-radius: 50%;
    min-width: 20px;
    height: 20px;
    display: flex;
    justify-content: center;
    align-items: center;
    z-index: 3;
    box-shadow: 0 2px 4px rgba(0,0,0,0.2);
  }

  /* Metro Style: Square Badge */
  :global(.v-metro .v-badge) {
    border-radius: 3px; 
    background: #fff;
    color: var(--v-color);
  }

  /* Tram Style: Slightly larger */
  :global(.v-tram .v-badge) {
    border-width: 2px;
  }

  /* --- POPUP STYLES --- */
  :global(.p-container) { 
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; 
      min-width: 160px; 
  }
  :global(.p-header) {    
      color: white; padding: 8px 12px; 
      font-weight: bold; border-radius: 4px 4px 0 0; 
      display: flex; align-items: center; gap: 8px;
  }
  :global(.p-line-badge) {
    background: rgba(255,255,255,0.2);
    padding: 1px 6px;
    border-radius: 4px;
    font-size: 0.9em;
    min-width: 18px;
    text-align: center;
  }
  :global(.p-body) { padding: 10px; line-height: 1.5em; font-size: 13px; color: #333; }
  :global(.p-row) { display: flex; justify-content: space-between; border-bottom: 1px solid #eee; padding: 2px 0; }
  :global(.p-row:last-child) { border-bottom: none; }
  :global(.maplibregl-popup-content) { padding: 0 !important; border-radius: 6px; overflow: hidden; box-shadow: 0 4px 15px rgba(0,0,0,0.2); }
  :global(.maplibregl-popup-close-button) { color: white; font-size: 16px; top: 0; right: 4px; padding: 4px; }
  :global(.maplibregl-popup-close-button:hover) { background: none; color: #eee; }
</style>
