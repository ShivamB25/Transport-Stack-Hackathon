import type { Health, JourneyParams, JourneyResponse, LiveStatus, ReplayDemo } from './types'

export class ApiError extends Error {
  readonly status: number
  readonly detail: string

  constructor(status: number, detail: string) {
    super(`Request failed (${status}): ${detail}`)
    this.status = status
    this.detail = detail
  }
}

function errorDetail(body: unknown, fallback: string): string {
  const detail = (body as { detail?: unknown } | null)?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail.map((item) => (item as { msg?: string }).msg ?? JSON.stringify(item)).join('; ')
  }
  return fallback
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { signal, headers: { Accept: 'application/json' } })
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null)
    throw new ApiError(response.status, errorDetail(body, response.statusText))
  }
  return (await response.json()) as T
}

export function getJourneys(params: JourneyParams, signal?: AbortSignal): Promise<JourneyResponse> {
  const query = new URLSearchParams({ ...params })
  return getJson<JourneyResponse>(`/journeys?${query}`, signal)
}

export function getReplayDemo(signal?: AbortSignal): Promise<ReplayDemo> {
  return getJson<ReplayDemo>('/replay/demo', signal)
}

export function getLiveStatus(signal?: AbortSignal): Promise<LiveStatus> {
  return getJson<LiveStatus>('/live/status', signal)
}

export function getHealth(signal?: AbortSignal): Promise<Health> {
  return getJson<Health>('/health', signal)
}
