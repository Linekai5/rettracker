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
        const speedKmh = Math.round((data.speed || 0) * 3.6);
        return `
            <div style="color:black; font-family:sans-serif; font-size:12px;">
                <strong>Route ${data.route_id}</strong><br>
                Speed: ${speedKmh} km/h<br>
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

        // Calculate distance from current animated position to new target
        const dist = this.getHaversineDistance(this.currentPos.lat, this.currentPos.lon, newData.lat, newData.lon);
        
        // Calculate duration based on speed (m/s)
        let duration = 2.0; 
        const speed = newData.speed || 0;
        
        // If we have a valid speed, use it to determine duration (distance / speed)
        if (speed > 0.1) {
            duration = dist / speed;
        } else if (dist > 50) {
             // Teleport or lost signal recovery
             duration = 2.0; 
        } else {
             // Stopped or very slow
             duration = 0.5; 
        }

        // Animate Position using calculated duration and linear ease for steady flow
        gsap.to(this.currentPos, {
            lat: newData.lat,
            lon: newData.lon,
            duration: duration, 
            ease: "none",
            onUpdate: () => {
                this.marker.setLngLat([this.currentPos.lon, this.currentPos.lat]);
            }
        });

        // Animate Bearing
        if (newData.bearing !== undefined) {
             const startBearing = this.currentBearing;
             const endBearing = newData.bearing;
             
             // Simple bearing interpolation
             const obj = { bearing: startBearing };
             gsap.to(obj, {
                 bearing: endBearing,
                 duration: 1.0, 
                 ease: "none",
                 onUpdate: () => {
                     this.marker.setRotation(obj.bearing);
                 }
             });
             this.currentBearing = endBearing;
        }
        
        // Update Popup
        this.popup.setHTML(this.getPopupContent(newData));
    }

    getHaversineDistance(lat1, lon1, lat2, lon2) {
        const R = 6371000; // Earth radius in meters
        const dLat = (lat2 - lat1) * Math.PI / 180;
        const dLon = (lon2 - lon1) * Math.PI / 180;
        const a = Math.sin(dLat/2) * Math.sin(dLat/2) +
                  Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
                  Math.sin(dLon/2) * Math.sin(dLon/2);
        const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
        return R * c;
    }

    remove() {
        if (this.marker) {
            this.marker.remove();
        }
        // Kill animations
        gsap.killTweensOf(this.currentPos);
    }
}
