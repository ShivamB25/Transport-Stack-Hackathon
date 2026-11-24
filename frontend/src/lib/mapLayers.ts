import L from 'leaflet'
import type { DemoLeg, DemoStage, JourneyOption, JourneyResponse, Leg, ReplayDemoReady } from '@/api/types'
import { fmtDuration } from './format'
import { decodePolyline } from './polyline'

const ROUTE_COLORS = ['#0f766e', '#2563eb', '#7c3aed', '#0284c7']

export type MapScene =
  | { kind: 'journey'; response: JourneyResponse; option: JourneyOption }
  | { kind: 'demo'; demo: ReplayDemoReady; stage: DemoStage }

/** Leaflet renders tooltip strings as HTML, so stop and route names are escaped. */
function escapeHtml(value: unknown): string {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

type Endpoints = Pick<Leg, 'from_lat' | 'from_lng' | 'to_lat' | 'to_lng' | 'geometry'>

/** The OTP geometry, or a straight segment between the leg endpoints when it is missing. */
function legCoords(leg: Endpoints): L.LatLngTuple[] {
  const coords = decodePolyline(leg.geometry)
  if (coords.length >= 2) return coords
  if (leg.from_lat != null && leg.from_lng != null && leg.to_lat != null && leg.to_lng != null) {
    return [
      [leg.from_lat, leg.from_lng],
      [leg.to_lat, leg.to_lng],
    ]
  }
  return []
}

function stopMarker(layer: L.FeatureGroup, lat: number, lng: number, color: string, radius: number, label: string) {
  L.circleMarker([lat, lng], { radius, color: '#ffffff', weight: 2, fillColor: color, fillOpacity: 1 })
    .bindTooltip(escapeHtml(label))
    .addTo(layer)
}

function busLine(layer: L.FeatureGroup, coords: L.LatLngTuple[], color: string, label: string, primary = true) {
  if (primary) L.polyline(coords, { color: '#ffffff', weight: 8, opacity: 0.85 }).addTo(layer)
  L.polyline(coords, {
    color,
    weight: primary ? 5 : 3.5,
    opacity: primary ? 0.95 : 0.65,
    dashArray: primary ? undefined : '8, 6',
  })
    .bindTooltip(escapeHtml(label))
    .addTo(layer)
}

/** Draws one journey option; returns the zoom cap for fitting the view. */
export function drawJourney(layer: L.FeatureGroup, response: JourneyResponse, option: JourneyOption): number {
  const markers: Parameters<typeof stopMarker>[] = []
  const { resolved_from: from, resolved_to: to } = response
  markers.push([layer, from.routing_lat, from.routing_lng, '#0f766e', 7, `Origin: ${from.stop_name}`])
  markers.push([layer, to.routing_lat, to.routing_lng, '#dc2626', 7, `Destination: ${to.stop_name}`])

  let busIndex = 0
  for (const leg of option.legs) {
    if (leg.type === 'transfer' && leg.from_lat && leg.from_lng) {
      const label = `Transfer: ${leg.from_stop || 'Stop'} -> ${leg.route_name || ''}`
      markers.push([layer, leg.from_lat, leg.from_lng, '#d97706', 6, label])
    }
    if (leg.type !== 'walk' && leg.type !== 'ride') continue
    const coords = legCoords(leg)
    if (coords.length < 2) continue
    if (leg.type === 'walk') {
      L.polyline(coords, { color: '#64748b', weight: 4, opacity: 0.85, dashArray: '6, 8' })
        .bindTooltip(escapeHtml(`Walk · ${fmtDuration(leg.estimated_duration_s)}`))
        .addTo(layer)
      continue
    }
    const color = ROUTE_COLORS[busIndex++ % ROUTE_COLORS.length]
    const route = leg.route_name || leg.route_id || ''
    const label = `Bus ${route}: ${leg.from_stop || ''} -> ${leg.to_stop || ''} (${fmtDuration(leg.estimated_duration_s)}, ${leg.source})`
    busLine(layer, coords, color, label)
    if (leg.from_lat != null && leg.from_lng != null) {
      markers.push([layer, leg.from_lat, leg.from_lng, color, 5, `Board ${leg.route_name || ''} at ${leg.from_stop || ''}`])
    }
    if (leg.to_lat != null && leg.to_lng != null) {
      markers.push([layer, leg.to_lat, leg.to_lng, color, 5, `Alight ${leg.route_name || ''} at ${leg.to_stop || ''}`])
    }
  }
  for (const marker of markers) stopMarker(...marker)
  return 15
}

function drawDemoLegs(layer: L.FeatureGroup, legs: DemoLeg[], color: string, primary: boolean, prefix: string) {
  for (const leg of legs) {
    const coords = legCoords(leg)
    if (coords.length < 2) continue
    if (!leg.transit) {
      L.polyline(coords, { color: '#94a3b8', weight: 3, opacity: primary ? 0.85 : 0.45, dashArray: '5, 7' }).addTo(layer)
    } else {
      busLine(layer, coords, color, `${prefix}: ${leg.route_name || 'BUS'}`, primary)
    }
  }
}

/** Draws both replay-demo itineraries, emphasising the one recommended at this stage, and the bus position. */
export function drawDemoStage(layer: L.FeatureGroup, demo: ReplayDemoReady, stage: DemoStage): number {
  const transferRecommended = stage.recommended_option === 'transfer'
  drawDemoLegs(layer, demo.geometries.direct_legs, transferRecommended ? '#64748b' : '#0f766e', !transferRecommended, 'Direct option')
  drawDemoLegs(layer, demo.geometries.transfer_legs, transferRecommended ? '#0f766e' : '#dc2626', transferRecommended, 'Transfer option')

  stopMarker(layer, demo.origin.lat, demo.origin.lng, '#0f766e', 7, `Origin: ${demo.origin.name}`)
  stopMarker(layer, demo.destination.lat, demo.destination.lng, '#2563eb', 7, `Destination: ${demo.destination.name}`)
  const xfer = demo.transfer_stop
  L.circleMarker([xfer.lat, xfer.lng], { radius: 8, color: '#fff', weight: 2.5, fillColor: '#d97706', fillOpacity: 1 })
    .bindTooltip(
      escapeHtml(
        `Transfer stop: ${xfer.stop_name} (connecting ${xfer.connecting_route} departs ` +
          `${xfer.connecting_observed_depart.slice(11, 19)} IST observed)`,
      ),
    )
    .addTo(layer)

  const delay = stage.observed_delay_s >= 0 ? `+${stage.observed_delay_s}s` : `${stage.observed_delay_s}s`
  const busIcon = L.divIcon({
    className: '',
    html: `<div class="bus-marker-icon">${escapeHtml(demo.route_name)} (${escapeHtml(delay)})</div>`,
    iconSize: [120, 24],
    iconAnchor: [60, 12],
  })
  L.marker([stage.bus_lat, stage.bus_lng], { icon: busIcon })
    .bindTooltip(escapeHtml(`Vehicle ${demo.vehicle_id} at ${stage.bus_stop_name} (${stage.replay_time_ist} IST)`))
    .addTo(layer)
  return 14
}

export function drawScene(layer: L.FeatureGroup, scene: MapScene): number {
  return scene.kind === 'journey'
    ? drawJourney(layer, scene.response, scene.option)
    : drawDemoStage(layer, scene.demo, scene.stage)
}
