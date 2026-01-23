export async function initMap(mapElement, geoJsonData) {
  const maplibreModule = await import('maplibre-gl');
  const maplibregl = maplibreModule.default || maplibreModule;

  // OpenFreeMap tile URL can be overridden via env
  // Use OpenFreeMap if explicitly configured via env, otherwise use CARTO dark_nolabels fallback
  const rawTileUrl = import.meta.env.VITE_OPENFREEMAP_URL || 'https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png';
  // MapLibre requires concrete tile URLs; expand {s} to subdomains and strip {r}
  let tilesArray;
  if (rawTileUrl.includes('{s}')) {
    const subs = ['a', 'b', 'c'];
    tilesArray = subs.map(s => rawTileUrl.replace('{s}', s).replace('{r}', ''));
  } else {
    tilesArray = [rawTileUrl.replace('{r}', '')];
  }

  // MapLibre expects [lng, lat]
  // Centered on Randstad (between R'dam and Den Haag)
  const initialCenter = [4.38, 51.98];
  const initialZoom = 10.5;
  
  const style = {
    version: 8,
    sources: {
      base: {
        type: 'raster',
        tiles: tilesArray,
        tileSize: 256,
        attribution: '© OpenFreeMap / OpenStreetMap contributors'
      }
    },
    layers: [
      { id: 'base', type: 'raster', source: 'base', minzoom: 0, maxzoom: 22 }
    ]
  };

  // Pre-inject GeoJSON data if provided (Critical Path Rendering)
  if (geoJsonData) {
      style.sources['ret-data'] = { type: 'geojson', data: geoJsonData };
      
      // 1. Bus Layer
      style.layers.push({
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
      style.layers.push({
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
      style.layers.push({
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
      style.layers.push({
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

  const map = new maplibregl.Map({
    container: mapElement,
    style,
    center: initialCenter,
    zoom: initialZoom,
    maxBounds: [[3.2, 50.7], [7.3, 53.6]], // Limit view to the Netherlands roughly
    interactive: true,
    attributionControl: false
  });

  // Ensure full-screen container background matches tiles to avoid white flash
  try { map.getContainer().style.backgroundColor = '#04142a'; } catch (e) {}

  return map;
}