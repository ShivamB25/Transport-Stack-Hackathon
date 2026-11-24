import type { AbstentionCode, JourneyResponse, LiveStatus, ReplayDemoReady } from '@/api/types'
import { fmtAge } from './format'

export interface BadgeState {
  mode: 'live' | 'replay'
  stale: boolean
  text: string
}

export const ABSTENTION_LABELS: Record<AbstentionCode, string> = {
  stale_feed: 'Stale GPS',
  feed_unavailable: 'GPS feed unavailable',
  insufficient_coverage: 'Insufficient GPS coverage',
  insufficient_route_coverage: 'Insufficient route coverage',
  replay_data_unavailable: 'Replay data unavailable',
}

function limit(threshold: number | null | undefined): string {
  return threshold ? ` (limit ${Math.round(threshold)}s)` : ''
}

function vehicles(count: number | null | undefined): string {
  return count ? ` · ${count} buses` : ''
}

export function badgeForJourney(journey: JourneyResponse): BadgeState {
  if (journey.status === 'insufficient_data') {
    const abstention = journey.abstention
    const label = (abstention && ABSTENTION_LABELS[abstention.code]) || 'Not enough data'
    const age = journey.feed_age_seconds != null ? ` · age ${fmtAge(journey.feed_age_seconds)}` : ''
    return { mode: journey.mode, stale: true, text: label + age + limit(abstention?.threshold_seconds) }
  }
  if (journey.mode === 'replay') {
    const reference = journey.reference_timestamp
      ? `${journey.reference_timestamp.slice(0, 16).replace('T', ' ')} IST`
      : 'October 2026'
    return { mode: 'replay', stale: false, text: `Replay: ${reference} · age ${fmtAge(journey.feed_age_seconds ?? 0)}` }
  }
  const count = journey.feed_summary?.fresh_vehicle_count
  return { mode: 'live', stale: false, text: `Feed age: ${fmtAge(journey.feed_age_seconds)}${vehicles(count)}` }
}

export function badgeForDemo(demo: ReplayDemoReady, stageIndex: number): BadgeState {
  const stage = demo.stages[stageIndex]
  return {
    mode: 'replay',
    stale: false,
    text: `Replay: ${demo.fixture_day} ${stage.replay_time_ist} IST · live feed not contacted`,
  }
}

export function badgeForLiveStatus(status: LiveStatus | undefined): BadgeState {
  if (!status) return { mode: 'live', stale: false, text: 'Checking feed status...' }
  const age = fmtAge(status.current_feed_age_seconds)
  if (status.is_fresh) {
    return { mode: 'live', stale: false, text: `Feed age: ${age}${vehicles(status.state?.fresh_vehicle_count)}` }
  }
  return { mode: 'live', stale: true, text: `Stale GPS · age ${age}${limit(status.abstention?.threshold_seconds)}` }
}
