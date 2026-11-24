import { afterEach, expect, test } from 'bun:test'
import { QueryClient, QueryObserver } from '@tanstack/react-query'
import { actionQueryOptions, type ActionResult } from '@/api/queries'
import type { JourneyParams } from '@/api/types'
import { IDLE_ACTION, nextAction } from './useLatestAction'

const realFetch = globalThis.fetch

afterEach(() => {
  globalThis.fetch = realFetch
})

function params(from: string): JourneyParams {
  return { from, to: 'Nehru Place', depart: '2026-11-24T08:30:00+05:30', mode: 'live' }
}

function journeyBody(from: string) {
  return { status: 'ok', mode: 'live', resolved_from: { stop_name: from }, options: [] }
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

function fromOf(result: ActionResult | undefined): string | undefined {
  return result?.kind === 'journey' ? result.journey.resolved_from.stop_name : result?.kind
}

test('a slow earlier response does not replace the newer action', async () => {
  const signals: Record<string, AbortSignal> = {}
  let slowArrived = false
  // The stub ignores aborts so the superseded response really arrives after the newer one.
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input)
    const from = url.startsWith('/journeys') ? new URL(url, 'http://x').searchParams.get('from')! : 'demo'
    signals[from] = init!.signal!
    if (from === 'Slow') {
      await sleep(60)
      slowArrived = true
    }
    const body = from === 'demo' ? { status: 'ok', stages: [] } : journeyBody(from)
    return new Response(JSON.stringify(body), { status: 200 })
  }) as typeof fetch

  const client = new QueryClient()
  const slow = nextAction(IDLE_ACTION, { kind: 'journey', params: params('Slow') })
  const observer = new QueryObserver(client, actionQueryOptions(slow))
  const seen: (string | undefined)[] = []
  const unsubscribe = observer.subscribe((result) => seen.push(fromOf(result.data)))

  const fast = nextAction(slow, { kind: 'journey', params: params('Fast') })
  observer.setOptions(actionQueryOptions(fast))
  await sleep(20)
  expect(fromOf(observer.getCurrentResult().data)).toBe('Fast')

  const demo = nextAction(fast, { kind: 'demo' })
  observer.setOptions(actionQueryOptions(demo))
  await sleep(100)

  expect(slowArrived).toBe(true)
  expect(signals.Slow.aborted).toBe(true)
  expect(fromOf(observer.getCurrentResult().data)).toBe('demo')
  expect(seen).not.toContain('Slow')
  unsubscribe()
  client.clear()
})

test('resubmitting identical inputs is a new action with its own request', () => {
  const first = nextAction(IDLE_ACTION, { kind: 'journey', params: params('A') })
  const again = nextAction(first, { kind: 'journey', params: params('A') })
  expect(actionQueryOptions(again).queryKey).not.toEqual(actionQueryOptions(first).queryKey)
})
