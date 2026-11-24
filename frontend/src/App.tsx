import { useCallback, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ApiError } from '@/api/client'
import { healthQuery, liveStatusQuery, type UserAction } from '@/api/queries'
import type { JourneyParams } from '@/api/types'
import { AbstentionPanel } from '@/components/AbstentionPanel'
import { JourneyForm } from '@/components/JourneyForm'
import { ModeBadge } from '@/components/ModeBadge'
import { OptionList } from '@/components/OptionList'
import { ReplayDemo } from '@/components/ReplayDemo'
import { RouteMap } from '@/components/RouteMap'
import { useLatestAction } from '@/hooks/useLatestAction'
import { badgeForDemo, badgeForJourney, badgeForLiveStatus } from '@/lib/badge'
import { normalizeDepart, REPLAY_DATE, withDate } from '@/lib/format'
import type { MapScene } from '@/lib/mapLayers'
import { DEFAULT_FORM } from '@/lib/presets'

function errorMessage(action: UserAction, error: Error): string {
  if (action.kind === 'demo') {
    return error instanceof ApiError
      ? `Failed to load replay demo: ${error.detail}`
      : `Network error loading replay demo: ${error.message}`
  }
  return error instanceof ApiError ? error.message : `Network error while requesting /journeys: ${error.message}`
}

export default function App() {
  const [form, setForm] = useState<JourneyParams>(DEFAULT_FORM)
  const [selectedOption, setSelectedOption] = useState(0)
  const [stageIndex, setStageIndex] = useState(0)
  const { action, start, result } = useLatestAction()
  const liveStatus = useQuery(liveStatusQuery)
  const health = useQuery(healthQuery)

  const plan = (values: JourneyParams) => {
    const params = { ...values, depart: normalizeDepart(values.depart) }
    setForm(params)
    setSelectedOption(0)
    start({ kind: 'journey', params })
  }
  const launchDemo = () => {
    setStageIndex(0)
    start({ kind: 'demo' })
  }
  const closeDemo = () => {
    start({ kind: 'idle' })
    void liveStatus.refetch()
  }
  const changeStage = useCallback((index: number) => setStageIndex(index), [])

  const data = result.data
  const journey = data?.kind === 'journey' ? data.journey : null
  const demo = data?.kind === 'demo' && data.demo.status === 'ok' ? data.demo : null
  const optionCount = journey?.status === 'ok' ? journey.options.length : 0
  const optionIndex = selectedOption < optionCount ? selectedOption : 0
  const demoStage = demo ? Math.min(stageIndex, demo.stages.length - 1) : 0

  let error: string | null = null
  if (result.error) error = errorMessage(action, result.error)
  else if (data?.kind === 'demo' && data.demo.status !== 'ok') error = data.demo.message || 'Replay demo unavailable.'

  const badge = journey
    ? badgeForJourney(journey)
    : demo
      ? badgeForDemo(demo, demoStage)
      : badgeForLiveStatus(liveStatus.data)

  const scene = useMemo<MapScene | null>(() => {
    if (journey?.status === 'ok' && journey.options.length > 0) {
      return { kind: 'journey', response: journey, option: journey.options[optionIndex] }
    }
    if (demo) return { kind: 'demo', demo, stage: demo.stages[demoStage] }
    return null
  }, [journey, optionIndex, demo, demoStage])

  const busy = result.isFetching

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-slate-50 text-sm text-slate-900">
      <header className="flex shrink-0 items-center justify-between gap-3 border-b border-slate-800 bg-slate-900 px-4.5 py-2.5 text-slate-50">
        <div className="flex flex-wrap items-baseline gap-2.5">
          <h1 className="text-[17px] font-bold tracking-tight">Delhi Bus Journey Planner</h1>
          <span className="text-xs text-slate-400">Model-timed routing · OTP2 candidates + real-time GPS</span>
        </div>
        <ModeBadge badge={badge} otpDown={health.data?.otp === 'down'} />
      </header>

      <div className="grid min-h-0 flex-1 grid-cols-[440px_1fr] overflow-hidden max-[900px]:grid-cols-1 max-[900px]:grid-rows-[52vh_1fr]">
        <aside className="sidebar flex flex-col gap-3.5 *:shrink-0 overflow-y-auto border-r border-slate-200 bg-white px-4 pt-3.5 pb-6">
          <JourneyForm
            values={form}
            onChange={setForm}
            onPlan={() => plan(form)}
            onReplayDemo={launchDemo}
            planning={busy && action.kind === 'journey'}
            loadingDemo={busy && action.kind === 'demo'}
          />

          {error && (
            <section id="error-panel" role="alert" className="rounded-lg border border-orange-300 bg-orange-50 px-3 py-2.5 text-[13px] text-orange-800">
              {error}
            </section>
          )}

          {journey?.status === 'insufficient_data' && (
            <AbstentionPanel
              response={journey}
              onRetry={() => plan(form)}
              onUseReplay={() => plan({ ...form, mode: 'replay', depart: withDate(form.depart, REPLAY_DATE) })}
            />
          )}

          {demo && <ReplayDemo demo={demo} stageIndex={demoStage} onStageChange={changeStage} onClose={closeDemo} />}

          {journey?.status === 'ok' && (
            <OptionList response={journey} selectedIndex={optionIndex} onSelect={setSelectedOption} />
          )}
        </aside>

        <RouteMap scene={scene} />
      </div>
    </div>
  )
}
