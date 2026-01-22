import gsap from 'gsap';

let maplibregl = null;

const METRO_COLORS = {
    'A': '#1ea245', // Green
    'B': '#ffce00', // Yellow
    'C': '#db002e', // Red
    'D': '#00a1de', // Light Blue
    'E': '#003e83'  // Dark Blue
};

const TRAM_COLOR = '#D100AA';
const BUS_COLOR = '#808080'; // Darker grey for visibility

export class Vehicle {
    static injectLibrary(lib) {
        maplibregl = lib;
    }

    constructor(data, map) {
        this.id = data.id;
        this.map = map;
        this.data = data;
        
        // State for animation
        this.currentPos = { lat: data.lat, lon: data.lon };
        this.currentBearing = data.bearing || 0;

        // Create DOM element
        this.element = document.createElement('div');
        this.element.className = 'vehicle-marker';
        this.element.style.width = '12px';
        this.element.style.height = '12px';
        this.element.style.borderRadius = '50%';
        this.element.style.border = '2px solid white';
        this.element.style.boxShadow = '0 0 4px rgba(0,0,0,0.5)';
        this.element.style.cursor = 'pointer';
        
        // Set initial color based on route
        this.setColor(this.getRouteColor(data.route_id));

        // Create Marker
        this.marker = new maplibregl.Marker({
            element: this.element,
            rotation: this.currentBearing,
            rotationAlignment: 'map' // Rotates with the map
        })
        .setLngLat([data.lon, data.lat])
        .addTo(map);

        // Optional: Popup
        this.popup = new maplibregl.Popup({ offset: 10, closeButton: false })
            .setHTML(this.getPopupContent(data));
            
        this.element.addEventListener('mouseenter', () => this.marker.setPopup(this.popup).togglePopup());
        this.element.addEventListener('mouseleave', () => this.popup.remove());
    }

    getRouteColor(routeId) {
        if (!routeId) return BUS_COLOR;
        
        // Check for Metro (A, B, C, D, E)
        // routeId might be "Line A" or just "A" or similar. 
        // Assuming simplistic check for now based on RET patterns.
        const upper = routeId.toUpperCase();
        for (const [line, color] of Object.entries(METRO_COLORS)) {
            if (upper.includes(line) && upper.length < 3) { // rudimentary check specifically for A-E
                 return color;
            }
        }

        // Trams usually numeric 1-25
        if (!isNaN(parseInt(routeId))) {
             return TRAM_COLOR;
        }

        return BUS_COLOR;
    }

    getPopupContent(data) {
        return `
            <div style="color:black; font-family:sans-serif; font-size:12px;">
                <strong>Route ${data.route_id}</strong><br>
                Speed: ${data.speed} km/h<br>
                Vehicle: ${data.label || data.id}
            </div>
        `;
    }

    setColor(color) {
        this.element.style.backgroundColor = color;
    }

    setSize(size) {
        this.element.style.width = `${size}px`;
        this.element.style.height = `${size}px`;
    }

    update(newData) {
        // Update Internal Data
        this.data = newData;
        
        // Animate Position (Lat/Lon)
        gsap.to(this.currentPos, {
            lat: newData.lat,
            lon: newData.lon,
            duration: 2.0, // Matches standard fetch interval roughly
            ease: "power1.inOut",
            onUpdate: () => {
                this.marker.setLngLat([this.currentPos.lon, this.currentPos.lat]);
            }
        });

        // Animate Bearing (handle 350 -> 10 wraparound if needed, but keeping simple for now)
        if (newData.bearing !== undefined) {
             gsap.to(this.marker.getElement(), {
                 rotation: newData.bearing,  // This might need specific CSS construct for marker rotation if marker module doesn't handle it during setRotation
                 duration: 1.0 
             });
             // MapLibre marker setRotation is instant, GSAP rotating the element style transform is separate.
             // Best to interpolate the value and call setRotation.
             
             // Simple bearing interpolation
             const startBearing = this.currentBearing;
             const endBearing = newData.bearing;
             
             // Shortest path rotation logic could go here
             
             const obj = { bearing: startBearing };
             gsap.to(obj, {
                 bearing: endBearing,
                 duration: 1.0,
                 onUpdate: () => {
                     this.marker.setRotation(obj.bearing);
                 }
             });
             this.currentBearing = endBearing;
        }
        
        // Update Popup
        this.popup.setHTML(this.getPopupContent(newData));
    }

    remove() {
        if (this.marker) {
            this.marker.remove();
        }
        // Kill animations
        gsap.killTweensOf(this.currentPos);
    }
}
