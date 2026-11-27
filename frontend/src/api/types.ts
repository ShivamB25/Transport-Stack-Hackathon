// Response shapes of the journeys API (backend/src/delhi_transit/api).

export type DataMode = 'live' | 'replay'
export type RequestMode = 'auto' | DataMode
export type LegType = 'walk' | 'board' | 'ride' | 'transfer' | 'alight'

export type AbstentionCode =
  | 'stale_feed'
  | 'feed_unavailable'
  | 'insufficient_coverage'
  | 'insufficient_route_coverage'
  | 'replay_data_unavailable'

export interface Leg {
  type: LegType
  route_id?: number | null
  route_name?: string | null
  from_stop?: string | null
  to_stop?: string | null
  from_stop_id?: string | null
  to_stop_id?: string | null
  from_lat?: number | null
  from_lng?: number | null
  to_lat?: number | null
  to_lng?: number | null
  scheduled_duration_s: number
  estimated_duration_s: number
  window_p10_s?: number | null
  window_p90_s?: number | null
  source: string
  timing_source?: string | null
  observed_coverage?: number | null
  observation_count?: number | null
  coverage_note?: string | null
  connection_missed_probability?: number | null
  next_bus_headway_s?: number | null
  geometry?: string | null
}

export interface JourneyOption {
  candidate_id: string
  legs: Leg[]
  total_duration_s: number
  total_window_p10_s: number
  total_window_p90_s: number
  quieter_hour_typical_s: number
  scenario_count: number
}

export interface ResolvedEndpoint {
  requested: string
  stop_id: string
  stop_name: string
  lat: number
  lng: number
  routing_lat: number
  routing_lng: number
}

export interface Abstention {
  label: string
  code: AbstentionCode
  reason: string
  feed_age_seconds?: number | null
  threshold_seconds?: number | null
  last_feed_timestamp?: string | null
  [detail: string]: unknown
}

export interface FeedSummary {
  fresh_vehicle_count?: number | null
  paused?: boolean
  [key: string]: unknown
}

export interface JourneyResponse {
  status: 'ok' | 'insufficient_data'
  mode: DataMode
  feed_age_seconds: number | null
  feed_timestamp: string | null
  reference_timestamp: string | null
  feed_summary: FeedSummary | null
  selected_rung: string
  depart: string
  resolved_from: ResolvedEndpoint
  resolved_to: ResolvedEndpoint
  options: JourneyOption[]
  abstention: Abstention | null
}

export interface JourneyParams {
  from: string
  to: string
  depart: string
  mode: RequestMode
}

export interface LiveStatus {
  paused: boolean
  poller_enabled: boolean
  auth_configured: boolean
  current_feed_age_seconds: number | null
  stale_threshold_seconds: number
  min_vehicles: number
  is_fresh: boolean
  abstention: Abstention | null
  state: { fresh_vehicle_count?: number | null; [key: string]: unknown } | null
}

export interface Health {
  status: 'ok' | 'degraded'
  otp: 'up' | 'down'
  feed: string
  feed_age_seconds: number | null
  selected_rung: string
  [key: string]: unknown
}

export interface DemoLeg {
  mode: string
  transit: boolean
  route_name: string | null
  from_name: string | null
  to_name: string | null
  from_lat: number | null
  from_lng: number | null
  to_lat: number | null
  to_lng: number | null
  duration_s: number
  geometry: string | null
}

export interface DemoOption {
  id: 'transfer' | 'direct'
  label: string
  scheduled_duration_s: number
  estimated_duration_s: number
  window_p10_s: number
  window_p90_s: number
  miss_probability: number
  recommended: boolean
}

export interface DemoStage {
  index: number
  stage_id: 'initial' | 'delay_growing' | 'advice_flip' | 'outcome_reveal'
  title: string
  status_badge: string
  headline: string
  explanation: string
  replay_timestamp: string
  replay_time_ist: string
  bus_stop_sequence: number
  bus_stop_name: string
  bus_lat: number
  bus_lng: number
  observed_delay_s: number
  observed_passage: boolean
  scheduled_transfer_gap_s: number
  remaining_buffer_s: number
  recommended_option: 'transfer' | 'direct'
  advice_flipped: boolean
  outcome_revealed: boolean
  actual_outcome: string | null
  missed_by_s: number | null
  transfer_option: DemoOption
  direct_option: DemoOption
}

export interface DemoPlace {
  name: string
  lat: number
  lng: number
  stop_id?: string
}

export interface ReplayDemoReady {
  status: 'ok'
  mode: 'replay'
  feed_age_seconds: number
  live_feed_contacted: false
  fixture_day: string
  run_id: string
  vehicle_id: string
  route_name: string
  depart: string
  origin: DemoPlace
  destination: DemoPlace
  transfer_stop: {
    stop_id: string
    stop_name: string
    lat: number
    lng: number
    connecting_route: string
    connecting_observed_depart: string
    actual_outcome: string
    missed_by_s: number
    [key: string]: unknown
  }
  geometries: { transfer_legs: DemoLeg[]; direct_legs: DemoLeg[] }
  stages: DemoStage[]
}

export interface ReplayDemoUnavailable {
  status: 'unavailable'
  mode: 'replay'
  live_feed_contacted: false
  fixture_day: string
  message: string
  stages: []
}

export type ReplayDemo = ReplayDemoReady | ReplayDemoUnavailable
