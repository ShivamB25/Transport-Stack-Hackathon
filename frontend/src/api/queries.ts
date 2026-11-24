import { queryOptions } from '@tanstack/react-query'
import { getHealth, getJourneys, getLiveStatus, getReplayDemo } from './client'
import type { JourneyParams, JourneyResponse, ReplayDemo } from './types'

export type ActionRequest = { kind: 'idle' } | { kind: 'journey'; params: JourneyParams } | { kind: 'demo' }

/** One user action drives the results pane; its id makes every click a fresh request. */
export type UserAction = ActionRequest & { id: number }

export type ActionResult = { kind: 'journey'; journey: JourneyResponse } | { kind: 'demo'; demo: ReplayDemo }

async function runAction(action: UserAction, signal: AbortSignal): Promise<ActionResult> {
  switch (action.kind) {
    case 'journey':
      return { kind: 'journey', journey: await getJourneys(action.params, signal) }
    case 'demo':
      return { kind: 'demo', demo: await getReplayDemo(signal) }
    case 'idle':
      throw new Error('an idle action has nothing to fetch')
  }
}

export function actionQueryOptions(action: UserAction) {
  return queryOptions({
    queryKey: ['action', action],
    queryFn: ({ signal }) => runAction(action, signal),
    enabled: action.kind !== 'idle',
    staleTime: Infinity,
    gcTime: 0,
    retry: false,
  })
}

export const liveStatusQuery = queryOptions({
  queryKey: ['live-status'],
  queryFn: ({ signal }) => getLiveStatus(signal),
  refetchInterval: 15_000,
  retry: false,
})

export const healthQuery = queryOptions({
  queryKey: ['health'],
  queryFn: ({ signal }) => getHealth(signal),
  refetchInterval: 30_000,
  retry: false,
})
