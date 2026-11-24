import type { ReactNode } from 'react'
import type { Leg } from '@/api/types'
import { fmtDuration, fmtDurationExact } from '@/lib/format'
import { cn } from '@/lib/utils'

const LEG_STYLES: Record<string, string> = {
  ride: 'border-l-teal-700 bg-teal-50',
  transfer: 'border-l-amber-600 bg-amber-50',
  board: 'border-l-blue-600 bg-blue-50',
  alight: 'border-l-slate-500 bg-slate-50',
  walk: 'border-l-slate-400 bg-slate-50',
}

const SOURCE_STYLES: Record<string, string> = {
  live: 'bg-green-100 text-green-800',
  replay: 'bg-indigo-100 text-indigo-800',
  historical: 'bg-amber-100 text-amber-800',
  timetable: 'bg-slate-200 text-slate-700',
}

function legLabel(leg: Leg): ReactNode {
  const route = leg.route_name ? <><strong>Bus {leg.route_name}</strong> · </> : null
  switch (leg.type) {
    case 'walk':
      return `${leg.from_stop || 'Start'} → ${leg.to_stop || 'Stop'}`
    case 'board':
      return <>{route}Board at {leg.from_stop || 'Stop'}</>
    case 'ride':
      return <>{route}{leg.from_stop || ''} → {leg.to_stop || ''}</>
    case 'transfer':
      return <>Transfer at {leg.from_stop || ''} to {leg.route_name ? <strong>{leg.route_name}</strong> : 'next bus'}</>
    case 'alight':
      return <>{route}Alight at {leg.to_stop || 'Stop'}</>
    default:
      return leg.type
  }
}

function hasWindow(leg: Leg): leg is Leg & { window_p10_s: number; window_p90_s: number } {
  return leg.window_p10_s != null && leg.window_p90_s != null && (leg.window_p90_s > 0 || leg.window_p10_s > 0)
}

function LegRow({ leg }: { leg: Leg }) {
  const source = leg.source || 'timetable'
  return (
    <div className={cn('flex flex-col gap-0.5 rounded-md border-l-3 px-2.5 py-1.5 text-xs', LEG_STYLES[leg.type])}>
      <div className="flex items-center justify-between gap-1.5 font-semibold">
        <span>
          <span className="mr-1 rounded bg-slate-200 px-1 py-px text-[10px] tracking-wide text-slate-700 uppercase">
            {leg.type}
          </span>{' '}
          {legLabel(leg)}
        </span>
        <span className="whitespace-nowrap tabular-nums">
          {leg.type === 'alight' ? (
            '0s'
          ) : (
            <>
              {fmtDurationExact(leg.estimated_duration_s)}{' '}
              <span className="font-normal text-slate-500">(sched {fmtDurationExact(leg.scheduled_duration_s)})</span>
            </>
          )}
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-slate-600">
        <span className={cn('source-tag rounded px-1.5 py-px text-[10px] font-bold', SOURCE_STYLES[source] ?? SOURCE_STYLES.timetable)}>
          {source}
          {leg.timing_source ? ` · ${leg.timing_source}` : ''}
        </span>
        {leg.observed_coverage != null && (
          <span>
            coverage: <strong>{Math.round(leg.observed_coverage * 100)}%</strong>
            {leg.observation_count != null ? ` (${leg.observation_count} obs)` : ''}
          </span>
        )}
        {hasWindow(leg) && (
          <span>
            window {fmtDurationExact(leg.window_p10_s)}–{fmtDurationExact(leg.window_p90_s)}
          </span>
        )}
        {leg.type === 'transfer' && leg.connection_missed_probability != null && (
          <>
            <span className="font-bold text-amber-700">miss risk {Math.round(leg.connection_missed_probability * 100)}%</span>
            {leg.next_bus_headway_s ? <span>next bus ~{fmtDuration(leg.next_bus_headway_s)}</span> : null}
          </>
        )}
      </div>
      {leg.coverage_note && <div className="text-[11px] text-slate-500">{leg.coverage_note}</div>}
    </div>
  )
}

export function LegBreakdown({ legs }: { legs: Leg[] }) {
  return (
    <div className="legs-breakdown mt-2.5 flex flex-col gap-1.5 border-t border-dashed border-slate-300 pt-2.5">
      {legs.map((leg, index) => (
        <LegRow key={index} leg={leg} />
      ))}
    </div>
  )
}
