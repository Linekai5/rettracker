export async function initMap(mapElement) {
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
  // Centered slightly more on Rotterdam
  const initialCenter = [4.47917, 51.9225]; 
  const initialZoom = 11;
  
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

  const map = new maplibregl.Map({
    container: mapElement,
    style,
    center: initialCenter,
    zoom: initialZoom,
    maxBounds: [[3.2, 50.7], [7.3, 53.6]], // Limit view to the Netherlands roughly
    interactive: true,
    dragRotate: false, // Disable 3D rotation (right-click / touchbar twist)
    touchPitch: false, // Disable touch pitch (two-finger vertical swipe)
    pitchWithRotate: false, // Lock pitch
    attributionControl: false
  });

  // Ensure full-screen container background matches tiles to avoid white flash
  try { map.getContainer().style.backgroundColor = '#04142a'; } catch (e) {}

  return map;
}