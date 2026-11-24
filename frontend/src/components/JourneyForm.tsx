import type { FormEvent } from 'react'
import type { JourneyParams, RequestMode } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select'
import { departForMode } from '@/lib/format'
import { PRESETS } from '@/lib/presets'

interface JourneyFormProps {
  values: JourneyParams
  onChange: (values: JourneyParams) => void
  onPlan: () => void
  onReplayDemo: () => void
  planning: boolean
  loadingDemo: boolean
}

export function JourneyForm({ values, onChange, onPlan, onReplayDemo, planning, loadingDemo }: JourneyFormProps) {
  const set = (patch: Partial<JourneyParams>) => onChange({ ...values, ...patch })
  const submit = (event: FormEvent) => {
    event.preventDefault()
    onPlan()
  }

  return (
    <Card size="sm" aria-label="Journey search">
      <CardContent>
        <form id="journey-form" className="flex flex-col gap-2.5" onSubmit={submit}>
          <div className="flex flex-col gap-1">
            <Label htmlFor="origin-input">Origin (stop name or lat,lng)</Label>
            <Input
              id="origin-input"
              name="from"
              required
              value={values.from}
              placeholder="e.g. Kashmere Gate ISBT or 28.6675,77.2282"
              onChange={(e) => set({ from: e.target.value })}
            />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="destination-input">Destination (stop name or lat,lng)</Label>
            <Input
              id="destination-input"
              name="to"
              required
              value={values.to}
              placeholder="e.g. Nehru Place or 28.5494,77.2510"
              onChange={(e) => set({ to: e.target.value })}
            />
          </div>
          <div className="grid grid-cols-[1fr_130px] gap-2">
            <div className="flex flex-col gap-1">
              <Label htmlFor="depart-input">Departure (ISO IST)</Label>
              <Input
                id="depart-input"
                name="depart"
                required
                value={values.depart}
                onChange={(e) => set({ depart: e.target.value })}
              />
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="mode-select">Data mode</Label>
              <NativeSelect
                id="mode-select"
                name="mode"
                className="w-full"
                value={values.mode}
                onChange={(e) => {
                  const mode = e.target.value as RequestMode
                  set({ mode, depart: departForMode(mode, values.depart) })
                }}
              >
                <NativeSelectOption value="auto">Auto</NativeSelectOption>
                <NativeSelectOption value="live">Live GPS</NativeSelectOption>
                <NativeSelectOption value="replay">Oct Replay</NativeSelectOption>
              </NativeSelect>
            </div>
          </div>

          <div className="flex flex-wrap gap-1.5" aria-label="Quick presets">
            {PRESETS.map((preset) => (
              <Button
                key={preset.id}
                id={preset.id}
                type="button"
                variant="outline"
                size="xs"
                className="rounded-full bg-slate-100 text-[11px] font-semibold text-slate-700"
                onClick={() => onChange(preset.apply(values))}
              >
                {preset.label}
              </Button>
            ))}
          </div>

          <div className="mt-0.5 flex gap-2">
            <Button id="plan-btn" type="submit" size="lg" className="flex-1 bg-teal-700 font-bold hover:bg-teal-800" disabled={planning}>
              {planning ? 'Planning...' : 'Plan journey'}
            </Button>
            <Button
              id="replay-demo-btn"
              type="button"
              size="lg"
              variant="outline"
              className="border-indigo-500 bg-indigo-50 font-bold text-indigo-800 hover:bg-indigo-100"
              disabled={loadingDemo}
              onClick={onReplayDemo}
            >
              {loadingDemo ? 'Loading replay...' : 'Replay demo'}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  )
}
