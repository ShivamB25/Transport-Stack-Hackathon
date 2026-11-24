import type { RequestMode } from '@/api/types'

const IST_OFFSET_MS = 5.5 * 3600 * 1000
const DEFAULT_DATE = '2026-11-24'
const DEFAULT_TIME = '08:30:00+05:30'
export const REPLAY_DATE = '2026-10-22'

function isMissing(value: number | null | undefined): value is null | undefined {
  return value === null || value === undefined || Number.isNaN(value)
}

/** Rounded duration for headlines: "45s", "12 min", "1h 5m". */
export function fmtDuration(seconds: number | null | undefined): string {
  if (isMissing(seconds)) return '--'
  const s = Math.round(seconds)
  if (s < 60) return `${s}s`
  const mins = Math.round(s / 60)
  if (mins < 60) return `${mins} min`
  return `${Math.floor(mins / 60)}h ${mins % 60}m`
}

/** Exact signed duration for leg rows: "-1m 5s", "40s". */
export function fmtDurationExact(seconds: number | null | undefined): string {
  if (isMissing(seconds)) return '--'
  const s = Math.round(seconds)
  const sign = s < 0 ? '-' : ''
  const abs = Math.abs(s)
  const m = Math.floor(abs / 60)
  return m === 0 ? `${sign}${abs % 60}s` : `${sign}${m}m ${abs % 60}s`
}

/** Feed age: seconds below two minutes, otherwise minutes with the raw seconds. */
export function fmtAge(seconds: number | null | undefined): string {
  if (isMissing(seconds)) return 'unavailable'
  const s = Math.round(seconds)
  if (s < 120) return `${s}s`
  return `${(s / 60).toFixed(1)} min (${s}s)`
}

/** Wall-clock HH:MM in Asia/Kolkata for `baseIso` plus `addSeconds`. */
export function fmtClockIST(baseIso: string, addSeconds = 0): string {
  const base = new Date(baseIso).getTime()
  if (Number.isNaN(base)) return '--:--'
  const ist = new Date(base + addSeconds * 1000 + IST_OFFSET_MS)
  const hh = String(ist.getUTCHours()).padStart(2, '0')
  const mm = String(ist.getUTCMinutes()).padStart(2, '0')
  return `${hh}:${mm}`
}

/** Completes "YYYY-MM-DDTHH:MM[:SS]" with seconds and the +05:30 offset; other input is passed through. */
export function normalizeDepart(value: string): string {
  const trimmed = value.trim()
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(trimmed)) return `${trimmed}:00+05:30`
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(trimmed)) return `${trimmed}+05:30`
  return trimmed
}

function datePart(depart: string): string {
  return depart.trim().match(/^(\d{4}-\d{2}-\d{2})/)?.[1] ?? DEFAULT_DATE
}

function timePart(depart: string): string {
  return depart.trim().match(/T(\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2})/)?.[1] ?? DEFAULT_TIME
}

/** Keeps the departure date, replacing the time of day ("09:00:00+05:30"). */
export function withTime(depart: string, time: string): string {
  return `${datePart(depart)}T${time}`
}

/** Keeps the time of day, moving the departure to `date`. */
export function withDate(depart: string, date: string): string {
  return `${date}T${timePart(depart)}`
}

/** Replay needs an October 2026 date and live a current one; other modes keep the departure. */
export function departForMode(mode: RequestMode, depart: string): string {
  const october = depart.trim().startsWith('2026-10-')
  if (mode === 'replay' && !october) return withDate(depart, REPLAY_DATE)
  if (mode === 'live' && october) return withDate(depart, DEFAULT_DATE)
  return depart
}
