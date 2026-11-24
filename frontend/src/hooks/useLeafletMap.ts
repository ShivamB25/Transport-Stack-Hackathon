import { useEffect, useState, type RefObject } from 'react'
import L from 'leaflet'

const DELHI_CENTER: L.LatLngTuple = [28.6139, 77.209]

export interface LeafletMap {
  map: L.Map
  /** Everything drawn for the current journey or replay stage; cleared before each redraw. */
  layer: L.FeatureGroup
}

/** Owns one plain Leaflet map with OpenStreetMap tiles inside `containerRef`. */
export function useLeafletMap(containerRef: RefObject<HTMLDivElement | null>): LeafletMap | null {
  const [leaflet, setLeaflet] = useState<LeafletMap | null>(null)

  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const map = L.map(container, { zoomControl: true }).setView(DELHI_CENTER, 11)
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap contributors',
    }).addTo(map)
    const layer = L.featureGroup().addTo(map)
    setLeaflet({ map, layer })
    return () => {
      setLeaflet(null)
      map.remove()
    }
  }, [containerRef])

  return leaflet
}
