import gsap from 'gsap';
import * as turf from '@turf/turf';

let maplibregl = null;

const METRO_COLORS = {
    'A': '#1ea245', // Green
    'B': '#ffce00', // Yellow
    'C': '#db002e', // Red
    'D': '#00a1de', // Light Blue
    'E': '#003e83'  // Dark Blue
};

const METRO_MAPPING = {
    "M006": "E",
    "M007": "C",
    "M008": "A",
    "M009": "B",
    "M010": "D"
};

const TRAM_COLOR = '#D100AA';
const BUS_COLOR = '#808080'; // Darker grey for visibility

export class Vehicle {
    static injectLibrary(lib) {
        maplibregl = lib;
    }

    constructor(data, map, routeGeometry) {
        this.id = data.id;
        this.map = map;
        this.routeGeometry = routeGeometry; // MultiLineString or LineString
        
        // Initial Snap
        let initialPos = [data.lon, data.lat];
        if (this.routeGeometry) {
             const snapped = turf.nearestPointOnLine(this.routeGeometry, initialPos);
             if (snapped && snapped.geometry && snapped.geometry.coordinates) {
                 initialPos = snapped.geometry.coordinates;
             }
        }
        
        this.data = data;
        
        // State for animation
        this.currentPos = { lat: initialPos[1], lon: initialPos[0] };
        
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
        // We use default rotationAlignment ('auto' -> 'viewport') to ensure the
        // circle always faces the screen and stays centered on the coordinate.
        // Rotation is removed because a circle has no visual direction.
        this.marker = new maplibregl.Marker({
            element: this.element
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
        
        // 1. Check Metro Mapping
        const mapped = METRO_MAPPING[routeId] || METRO_MAPPING[routeId.toUpperCase()];
        if (mapped && METRO_COLORS[mapped]) {
            return METRO_COLORS[mapped];
        }

        // 2. Fallback check for "Line A", "A", etc.
        const upper = routeId.toUpperCase();
        for (const [line, color] of Object.entries(METRO_COLORS)) {
            // Avoid matching "123" with "A" or similar weirdness, assume lines are single letters or "Metro X"
            if (upper === line || upper === `METRO ${line}`) { 
                 return color;
            }
        }

        // Trams usually numeric 1-25. 
        // We assume anything purely numeric < 100 is likely a Tram in RET (usually), BUT buses also have numbers.
        // RET Trams: 2, 4, 7, 8, 20, 21, 23, 24, 25.
        // RET Buses: 30+, 100+, etc.
        const num = parseInt(routeId);
        if (!isNaN(num)) {
             if (num < 30) return TRAM_COLOR; // Heuristic for Tram
             return BUS_COLOR; // Heuristic for Bus
        }

        return BUS_COLOR;
    }

    getPopupContent(data) {
        // --- Speed Calculation & Sanity Check ---
        let speedKmh = Math.round((data.speed || 0) * 3.6);
        // If speed is unreasonably high (e.g. > 130km/h), show N/A or cap it.
        // RET Metros max ~100km/h.
        if (speedKmh > 130) {
            speedKmh = "N/A"; // Likely a GPS jump or glitch
        } else {
            speedKmh = `${speedKmh} km/h`;
        }

        // --- Determine Vehicle Type & Label ---
        let type = "Bus"; 
        let label = data.line_hint || data.route_id; // Default to hint or route ID

        // Check Metro
        let metroLetter = METRO_MAPPING[data.route_id] || METRO_MAPPING[label];
        if (metroLetter) {
            type = "Metro";
            label = metroLetter;
        } else if (label && !isNaN(parseInt(label))) {
            const num = parseInt(label);
            if (num < 30) {
                type = "Tram";
            } else {
                type = "Bus";
            }
        }

        // --- Construct Display String ---
        // "Metro E", "Tram 25", "Bus 38"
        const displayTitle = `${type} ${label}`;
        const vehicleNum = data.label || data.id;

        return `
            <div style="color:black; font-family:sans-serif; font-size:13px; min-width:120px; padding:5px;">
                <div style="font-weight:bold; font-size:14px; margin-bottom:4px; border-bottom:1px solid #ccc; padding-bottom:2px;">
                    ${displayTitle}
                </div>
                <!-- <div style="font-style:italic; color:#555; margin-bottom:4px;">To: ${data.headsign || "Unknown Terminus"}</div> -->
                <div>Speed: <strong>${speedKmh}</strong></div>
                <div style="color:#666; font-size:11px; margin-top:4px;">Veh #: ${vehicleNum}</div>
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

    hasRouteGeometry() {
        return !!this.routeGeometry;
    }

    setRouteGeometry(geometry) {
        this.routeGeometry = geometry;
        // Immediate snap to new geometry if possible
        if (this.currentPos && this.currentPos.lat && this.currentPos.lon && this.routeGeometry) {
             const snapped = turf.nearestPointOnLine(this.routeGeometry, [this.currentPos.lon, this.currentPos.lat]);
             if (snapped && snapped.geometry && snapped.geometry.coordinates) {
                 // Force update visuals immediately
                 this.marker.setLngLat(snapped.geometry.coordinates);
                 this.currentPos.lon = snapped.geometry.coordinates[0];
                 this.currentPos.lat = snapped.geometry.coordinates[1];
             }
        }
    }

    update(newData) {
        // 1. FILTER INVALID COORDINATES
        // If the backend sends 0,0, it's an error/lost signal. 
        // Do not update position, do not animate. 
        // Just update metadata (popup) or return.
        if (!newData.lat || !newData.lon || (Math.abs(newData.lat) < 1 && Math.abs(newData.lon) < 1)) {
            // Coordinate invalid.
            // Only update popup content if we want, or just ignore.
            // We can optionally hide the marker, but for now just ignoring the move is safer to avoid jumps.
            return;
        }

        // Update Internal Data
        this.data = newData;

        // Snap Target
        let targetLon = newData.lon;
        let targetLat = newData.lat;
        
        // 2. SNAP CHECK
        // If routeGeometry is available, snap the TARGET coordinate to the line.
        if (this.routeGeometry) {
             const snapped = turf.nearestPointOnLine(this.routeGeometry, [newData.lon, newData.lat]);
             if (snapped && snapped.geometry && snapped.geometry.coordinates) {
                 targetLon = snapped.geometry.coordinates[0];
                 targetLat = snapped.geometry.coordinates[1];
             }
        }

        // 3. CHECK FOR "POP IN" (Initial or Recovery)
        // If currentPos is effectively 0,0 (initialized bad) OR distance is huge, 
        // do not interpolate. Teleport instantly.
        if (Math.abs(this.currentPos.lat) < 1 || Math.abs(this.currentPos.lon) < 1) {
             this.currentPos.lat = targetLat;
             this.currentPos.lon = targetLon;
             this.marker.setLngLat([targetLon, targetLat]);
             return; // No animation needed for instant pop-in
        }

        // Calculate distance from current animated position to new target
        const dist = this.getHaversineDistance(this.currentPos.lat, this.currentPos.lon, targetLat, targetLon);

        // 4. ANIMATION LOGIC
        // If distance is huge (>500m), it's a teleport/reset. Don't slide across map.
        if (dist > 500) {
             gsap.killTweensOf(this.currentPos);
             this.currentPos.lat = targetLat;
             this.currentPos.lon = targetLon;
             this.marker.setLngLat([targetLon, targetLat]);
             return;
        }
        
        // Calculate duration based on speed (m/s)
        let duration = 2.0; 
        const speed = newData.speed || 0;
        
        // If we have a valid speed, use it to determine duration (distance / speed)
        if (speed > 0.1) {
            duration = dist / speed;
            // Cap duration to avoid super slow drift if speed is remarkably low for distance
            if (duration > 10.0) duration = 10.0; 
        } else if (dist > 50) {
             // Teleport or lost signal recovery
             duration = 2.0; 
        } else {
             // Stopped or very slow
             duration = 0.5; 
        }
        
        // Safety cap on duration against fetch interval
        // If duration is too long compared to updates, we'll always lag.
        // Assuming ~2s updates, allowing up to 3s smooths it out.
        // But if speed says 30s, we should probably just move faster to catch up.
        if (duration > 3.0) duration = 3.0;

        // Animate Position using calculated duration and linear ease for steady flow
        gsap.to(this.currentPos, {
            lat: targetLat,
            lon: targetLon,
            duration: duration, 
            ease: "none",
            onUpdate: () => {
                let displayLon = this.currentPos.lon;
                let displayLat = this.currentPos.lat;

                // Continuous Snap
                // This ensures that even if the linear interpolation between A and B
                // cuts a corner, we project the marker back onto the track for every frame.
                if (this.routeGeometry) {
                     // We snap the current animated position to the route line
                     const snapped = turf.nearestPointOnLine(this.routeGeometry, [displayLon, displayLat]);
                     if (snapped && snapped.geometry && snapped.geometry.coordinates) {
                         // Only apply continuous snap if reasonably close (e.g. < 50m)
                         // optimizing for visual correctness without wild jumps if geometry is looped
                         // But for now, strict snapping is requested.
                         displayLon = snapped.geometry.coordinates[0];
                         displayLat = snapped.geometry.coordinates[1];
                     }
                }
                
                this.marker.setLngLat([displayLon, displayLat]);
            }
        });

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
