import { useEffect, useRef } from 'react'
import { useLeafletMap } from '@/hooks/useLeafletMap'
import { drawScene, type MapScene } from '@/lib/mapLayers'

const LEGEND = [
  { label: 'Bus leg 1 / Recommended', style: { background: '#0f766e' } },
  { label: 'Bus leg 2 (transfer)', style: { background: '#2563eb' } },
  { label: 'Infeasible / alternative', style: { background: '#dc2626', height: 3 } },
  { label: 'Walk leg', style: { borderTop: '3px dashed #64748b', background: 'transparent' } },
]

/** Redraws whenever `scene` changes identity, so callers memoise it. */
export function RouteMap({ scene }: { scene: MapScene | null }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const leaflet = useLeafletMap(containerRef)

  useEffect(() => {
    if (!leaflet) return
    const { map, layer } = leaflet
    layer.clearLayers()
    if (!scene) return
    const maxZoom = drawScene(layer, scene)
    const bounds = layer.getBounds()
    if (bounds.isValid()) map.fitBounds(bounds, { padding: [36, 36], maxZoom })
  }, [leaflet, scene])

  return (
    <main className="relative isolate h-full w-full">
      <div id="map" ref={containerRef} role="region" aria-label="Delhi transit map" className="h-full w-full" />
      <div className="absolute right-4 bottom-4 z-[500] rounded-lg border border-slate-300 bg-white/95 px-2.5 py-2 text-[11px] text-slate-700 shadow-md">
        {LEGEND.map((item) => (
          <div key={item.label} className="mb-0.5 flex items-center gap-1.5 last:mb-0">
            <span className="inline-block h-1 w-[18px] rounded-sm" style={item.style} />
            {item.label}
          </div>
        ))}
      </div>
    </main>
  )
}
