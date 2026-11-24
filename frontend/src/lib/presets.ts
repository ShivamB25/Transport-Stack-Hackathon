import type { JourneyParams } from '@/api/types'
import { withTime } from './format'

export const DEFAULT_FORM: JourneyParams = {
  from: 'Kashmere Gate ISBT',
  to: 'Nehru Place',
  depart: '2026-11-24T08:30:00+05:30',
  mode: 'auto',
}

export interface Preset {
  id: string
  label: string
  apply: (values: JourneyParams) => JourneyParams
}

// October replay passages exist only for route 717A (UP 2171 / DOWN 2170).
export const PRESETS: Preset[] = [
  {
    id: 'preset-example-live',
    label: 'Example Tue 08:30 (Live)',
    apply: () => ({ ...DEFAULT_FORM, mode: 'live' }),
  },
  {
    id: 'preset-example-replay',
    label: 'Oct 22 08:30 717A (Replay)',
    apply: () => ({ from: 'Badarpur Border (T)', to: 'Kapashera Border', depart: '2026-10-22T08:30:00+05:30', mode: 'replay' }),
  },
  {
    id: 'preset-replay-abstain',
    label: 'Oct 22 08:30 no 717A (Abstains)',
    apply: () => ({ ...DEFAULT_FORM, depart: '2026-10-22T08:30:00+05:30', mode: 'replay' }),
  },
  { id: 'preset-peak', label: '09:00 Peak', apply: (v) => ({ ...v, depart: withTime(v.depart, '09:00:00+05:30') }) },
  { id: 'preset-quiet', label: '23:00 Quiet', apply: (v) => ({ ...v, depart: withTime(v.depart, '23:00:00+05:30') }) },
]
