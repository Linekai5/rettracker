<script>
  import { onMount, onDestroy } from 'svelte';
  import { initMap } from './map.js';
  import { browser } from '$app/environment';

  let mapElement;
  let map;

  onMount(async () => {
    if (!browser) return; // Veiligheid, alleen in browser

    map = await initMap(mapElement);

    // Belangrijk: forceer Leaflet om de grootte te herkennen
    setTimeout(() => {
      if (map) map.invalidateSize();
    }, 100);

    console.log('Kaart geladen en resized!');
  });

  // Optioneel: resize bij window resize (voor mobiel etc.)
  function handleResize() {
    if (map) map.invalidateSize();
  }

  if (browser) {
    window.addEventListener('resize', handleResize);
  }

  onDestroy(() => {
    if (browser) {
      window.removeEventListener('resize', handleResize);
    }
  });
</script>

<div bind:this={mapElement} class="map-container"></div>

<style>
  @import '../lib/style.css';
</style>