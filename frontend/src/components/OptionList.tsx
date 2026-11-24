import type { KeyboardEvent, ReactNode } from 'react'
import type { JourneyOption, JourneyResponse } from '@/api/types'
import { fmtClockIST, fmtDuration } from '@/lib/format'
import { cn } from '@/lib/utils'
import { LegBreakdown } from './LegBreakdown'

function ResultsSummary({ response }: { response: JourneyResponse }) {
  const { resolved_from: from, resolved_to: to } = response
  return (
    <div id="results-summary" className="flex flex-col gap-1 rounded-lg bg-slate-100 px-3 py-2 text-xs text-slate-600">
      <div>
        <strong className="text-slate-900">{from.stop_name}</strong> (#{from.stop_id}) →{' '}
        <strong className="text-slate-900">{to.stop_name}</strong> (#{to.stop_id})
      </div>
      <div>
        Depart {response.depart} · <strong className="text-slate-900">{response.options.length} options</strong> · model rung{' '}
        {response.selected_rung} · mode <strong className="text-slate-900">{response.mode}</strong>
      </div>
    </div>
  )
}

/** The scenario median compared with the typical duration at the 23:00 quiet hour. */
function QuietHourDelta({ option }: { option: JourneyOption }) {
  const diff = option.total_duration_s - option.quieter_hour_typical_s
  if (Math.abs(diff) < 30) return <span className="font-semibold text-green-700">same as quiet hour</span>
  if (diff > 0) return <span className="font-semibold text-amber-700">+{fmtDuration(diff)} slower vs 23:00</span>
  return <span className="font-semibold text-green-700">{fmtDuration(-diff)} faster vs 23:00</span>
}

function Metric({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex justify-between gap-2">
      <span>{label}</span>
      {children}
    </div>
  )
}

interface OptionCardProps {
  option: JourneyOption
  index: number
  depart: string
  selected: boolean
  onSelect: () => void
}

function OptionCard({ option, index, depart, selected, onSelect }: OptionCardProps) {
  const rides = option.legs.filter((leg) => leg.type === 'ride')
  const transfers = option.legs.filter((leg) => leg.type === 'transfer').length
  const transferLabel = transfers === 0 ? 'Direct' : `${transfers} ${transfers === 1 ? 'transfer' : 'transfers'}`
  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      onSelect()
    }
  }

  return (
    <article
      className={cn(
        'option-card cursor-pointer rounded-xl border-[1.5px] bg-white p-3 transition-[border-color,box-shadow] hover:border-slate-400',
        selected ? 'selected border-teal-700 shadow-[0_0_0_2px_rgba(15,118,110,0.15)] hover:border-teal-700' : 'border-slate-200',
      )}
      data-option-index={index}
      tabIndex={0}
      aria-selected={selected}
      onClick={onSelect}
      onKeyDown={onKeyDown}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex flex-wrap items-center gap-1.5 text-[13px] font-bold">
          Option {index + 1}{' '}
          {rides.map((ride, i) => (
            <span key={i} className="contents">
              {i > 0 && <span>→</span>}
              <span className="rounded-md bg-teal-700 px-1.5 py-0.5 text-[11px] text-white">{ride.route_name || `Route ${ride.route_id}`}</span>
            </span>
          ))}
          <span className="rounded-md bg-slate-100 px-1.5 py-0.5 text-[11px] font-semibold text-slate-600">{transferLabel}</span>
        </div>
        <div className="text-right whitespace-nowrap">
          <span className="text-lg font-extrabold tabular-nums">{fmtDuration(option.total_duration_s)}</span>
          <span className="block text-[11px] text-slate-500 tabular-nums">
            {option.total_duration_s}s ({(option.total_duration_s / 60).toFixed(1)}m)
          </span>
        </div>
      </div>
      <div className="mt-2 grid gap-1 border-t border-slate-100 pt-2 text-xs text-slate-600">
        <Metric label="Arrival (p10–p90 window)">
          <strong className="text-slate-900 tabular-nums">
            {fmtClockIST(depart, option.total_duration_s)} ({fmtClockIST(depart, option.total_window_p10_s)} –{' '}
            {fmtClockIST(depart, option.total_window_p90_s)})
          </strong>
        </Metric>
        <Metric label="Trip duration window">
          <strong className="text-slate-900 tabular-nums">
            {fmtDuration(option.total_window_p10_s)} – {fmtDuration(option.total_window_p90_s)}
          </strong>
        </Metric>
        <Metric label="Quiet-hour typical (23:00)">
          <span>
            <strong className="text-slate-900">{fmtDuration(option.quieter_hour_typical_s)}</strong> · <QuietHourDelta option={option} />
          </span>
        </Metric>
      </div>
      {selected ? (
        <LegBreakdown legs={option.legs} />
      ) : (
        <div className="mt-1.5 text-[11px] font-semibold text-teal-700">Click to inspect leg breakdown &amp; show on map</div>
      )}
    </article>
  )
}

interface OptionListProps {
  response: JourneyResponse
  selectedIndex: number
  onSelect: (index: number) => void
}

export function OptionList({ response, selectedIndex, onSelect }: OptionListProps) {
  return (
    <>
      <ResultsSummary response={response} />
      <section id="options-list" className="flex flex-col gap-2.5" aria-label="Journey options">
        {response.options.map((option, index) => (
          <OptionCard
            key={option.candidate_id || index}
            option={option}
            index={index}
            depart={response.depart}
            selected={index === selectedIndex}
            onSelect={() => onSelect(index)}
          />
        ))}
      </section>
    </>
  )
}
