import { useEffect, useState, type ReactNode } from 'react'
import type { DemoOption, DemoStage, ReplayDemoReady } from '@/api/types'
import { Button } from '@/components/ui/button'
import { fmtDuration, fmtDurationExact } from '@/lib/format'
import { cn } from '@/lib/utils'

const AUTOPLAY_INTERVAL_MS = 2200

function bannerTone(stage: DemoStage): string {
  if (stage.outcome_revealed) return 'outcome border-l-violet-600 bg-violet-50 text-violet-900'
  if (stage.advice_flipped) return 'flipped border-l-red-600 bg-red-50 text-red-800'
  if (stage.stage_id === 'delay_growing') return 'warning border-l-amber-600 bg-amber-50 text-amber-800'
  return 'border-l-teal-700 bg-emerald-50 text-emerald-800'
}

/** Green when on track, amber when getting close, red when late. */
function severity(bad: boolean, warn: boolean): string {
  return bad ? 'text-red-600' : warn ? 'text-amber-600' : 'text-teal-700'
}

function Telemetry({ stage }: { stage: DemoStage }) {
  const delay = stage.observed_delay_s
  const buffer = stage.remaining_buffer_s
  const boxes = [
    { id: 'tel-time', label: 'Replay Time', value: stage.replay_time_ist, tone: '' },
    {
      id: 'tel-delay',
      label: '717A Delay',
      value: `${delay >= 0 ? '+' : ''}${fmtDurationExact(delay)}`,
      tone: severity(delay > 300, delay > 120),
    },
    {
      id: 'tel-buffer',
      label: 'Xfer Buffer',
      value: buffer >= 0 ? `${fmtDurationExact(buffer)} left` : `${fmtDurationExact(Math.abs(buffer))} late`,
      tone: severity(buffer < 0, buffer < 180),
    },
  ]
  return (
    <div className="grid grid-cols-3 gap-1.5">
      {boxes.map((box) => (
        <div key={box.id} className="rounded-md border border-slate-200 bg-white px-2 py-1.5 text-center">
          <div className="text-[10px] font-bold text-slate-500 uppercase">{box.label}</div>
          <div id={box.id} className={cn('text-sm font-extrabold tabular-nums', box.tone)}>
            {box.value}
          </div>
        </div>
      ))}
    </div>
  )
}

function Pill({ tone, children }: { tone: 'yes' | 'no' | 'alt'; children: string }) {
  const tones = { yes: 'bg-green-100 text-green-800', no: 'bg-red-100 text-red-800', alt: 'bg-slate-200 text-slate-600' }
  return <span className={cn('rounded-full px-2 py-0.5 text-[10px] font-extrabold uppercase', tones[tone])}>{children}</span>
}

interface DemoOptionCardProps {
  option: DemoOption
  pill: ReactNode
  missRisk: string
  /** The revealed outcome is one observed time, not a predicted window. */
  actual?: boolean
}

function DemoOptionCard({ option, pill, missRisk, actual = false }: DemoOptionCardProps) {
  return (
    <div
      className={cn(
        'demo-opt-card rounded-lg border-[1.5px] bg-white px-3 py-2',
        option.recommended && 'recommended border-teal-700 bg-teal-50',
        !option.recommended && option.id === 'transfer' && 'infeasible border-red-300 bg-red-50',
        !option.recommended && option.id === 'direct' && 'border-slate-200',
      )}
    >
      <div className="flex items-center justify-between gap-1.5 text-xs font-bold">
        <span>{option.label}</span>
        {pill}
      </div>
      {actual ? (
        <div className="mt-1 text-xs text-slate-700">
          Actual: <strong>{fmtDuration(option.estimated_duration_s)}</strong> · Timetable:{' '}
          {fmtDuration(option.scheduled_duration_s)}
        </div>
      ) : (
        <div className="mt-1 text-xs text-slate-700">
          Est. <strong>{fmtDuration(option.estimated_duration_s)}</strong> (window {fmtDuration(option.window_p10_s)}–
          {fmtDuration(option.window_p90_s)}) · Timetable: {fmtDuration(option.scheduled_duration_s)} · Miss risk:{' '}
          <strong>{missRisk}</strong>
        </div>
      )}
    </div>
  )
}

function StageOptions({ stage }: { stage: DemoStage }) {
  const transfer = stage.transfer_option
  const direct = stage.direct_option
  const transferPill = stage.outcome_revealed ? (
    <Pill tone="no">{`ACTUAL: ${(stage.actual_outcome || 'missed').toUpperCase()}`}</Pill>
  ) : transfer.recommended ? (
    <Pill tone="yes">RECOMMENDED</Pill>
  ) : (
    <Pill tone="no">INFEASIBLE</Pill>
  )
  const directPill = direct.recommended ? (
    <Pill tone="yes">{stage.advice_flipped ? 'RECOMMENDED (FLIPPED)' : 'RECOMMENDED'}</Pill>
  ) : (
    <Pill tone="alt">BACKUP DIRECT</Pill>
  )
  return (
    <div id="demo-options" className="flex flex-col gap-2">
      <DemoOptionCard
        option={transfer}
        pill={transferPill}
        missRisk={`${Math.round(transfer.miss_probability * 100)}%`}
        actual={stage.outcome_revealed}
      />
      <DemoOptionCard option={direct} pill={directPill} missRisk="0% (direct)" />
    </div>
  )
}

interface ReplayDemoProps {
  demo: ReplayDemoReady
  stageIndex: number
  onStageChange: (index: number) => void
  onClose: () => void
}

/** Steps through the scripted October 22 missed-transfer replay: advice, delay, flip, observed outcome. */
export function ReplayDemo({ demo, stageIndex, onStageChange, onClose }: ReplayDemoProps) {
  const [autoplay, setAutoplay] = useState(false)
  const last = demo.stages.length - 1
  const stage = demo.stages[stageIndex]

  const playing = autoplay && stageIndex < last

  useEffect(() => {
    if (!playing) return
    const timer = setTimeout(() => onStageChange(stageIndex + 1), AUTOPLAY_INTERVAL_MS)
    return () => clearTimeout(timer)
  }, [playing, stageIndex, onStageChange])

  const goTo = (index: number) => {
    setAutoplay(false)
    onStageChange(Math.max(0, Math.min(index, last)))
  }
  const toggleAutoplay = () => {
    if (playing) {
      setAutoplay(false)
      return
    }
    if (stageIndex >= last) onStageChange(0)
    setAutoplay(true)
  }

  return (
    <section
      id="replay-demo-panel"
      aria-label="Marginal-transfer replay demo"
      className="flex flex-col gap-2.5 rounded-xl border-2 border-indigo-500 bg-[#fafaff] p-3"
    >
      <div className="flex items-start justify-between gap-2">
        <div>
          <h2 className="text-[15px] font-bold text-indigo-950">Marginal-Transfer Replay Demo</h2>
          <div id="replay-demo-sub" className="mt-0.5 text-[11px] text-indigo-700">
            Real {demo.fixture_day} GPS · Run {demo.run_id} (Vehicle {demo.vehicle_id}) · {demo.origin.name} -&gt;{' '}
            {demo.destination.name}
          </div>
        </div>
        <Button id="close-demo-btn" type="button" variant="outline" size="sm" onClick={onClose}>
          Close
        </Button>
      </div>

      <div id="stage-tabs" className="grid grid-cols-2 gap-1.5">
        {demo.stages.map((s, index) => (
          <button
            key={s.stage_id}
            type="button"
            aria-pressed={index === stageIndex}
            className={cn(
              'stage-tab rounded-md border px-2 py-1.5 text-left text-[11px] font-bold',
              index === stageIndex ? 'active border-indigo-700 bg-indigo-600 text-white' : 'border-indigo-200 bg-white text-indigo-800',
            )}
            onClick={() => goTo(index)}
          >
            {s.title}
          </button>
        ))}
      </div>

      <div id="advice-banner" className={cn('advice-banner rounded-lg border-l-4 px-3 py-2.5', bannerTone(stage))}>
        <div id="advice-status" className="mb-0.5 text-[11px] font-extrabold tracking-wide">
          {stage.status_badge}
        </div>
        <div id="advice-headline" className="mb-1 text-sm font-extrabold">
          {stage.headline}
        </div>
        <div id="advice-explanation" className="text-xs leading-relaxed">
          {stage.explanation}
        </div>
      </div>

      <Telemetry stage={stage} />
      <StageOptions stage={stage} />

      <div className="flex flex-wrap gap-1.5">
        <Button id="demo-prev-btn" type="button" variant="outline" size="sm" disabled={stageIndex === 0} onClick={() => goTo(stageIndex - 1)}>
          Previous
        </Button>
        <Button
          id="demo-next-btn"
          type="button"
          size="sm"
          className="flex-1 bg-teal-700 hover:bg-teal-800"
          disabled={stageIndex === last}
          onClick={() => goTo(stageIndex + 1)}
        >
          Next step
        </Button>
        <Button
          id="demo-autoplay-btn"
          type="button"
          size="sm"
          variant="outline"
          className="border-indigo-500 bg-indigo-50 text-indigo-800"
          onClick={toggleAutoplay}
        >
          {playing ? 'Pause' : 'Auto-play'}
        </Button>
      </div>
    </section>
  )
}
