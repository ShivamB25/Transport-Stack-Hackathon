import { useCallback, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { actionQueryOptions, type ActionRequest, type UserAction } from '@/api/queries'

export const IDLE_ACTION: UserAction = { id: 0, kind: 'idle' }

/** Every request supersedes the current action, including an identical resubmit. */
export function nextAction(current: UserAction, request: ActionRequest): UserAction {
  return { ...request, id: current.id + 1 }
}

/**
 * The latest user action wins: the results pane reads only the query of the newest action.
 * A superseded request is aborted, and if its response still arrives it lands under a key nobody observes.
 */
export function useLatestAction() {
  const [action, setAction] = useState<UserAction>(IDLE_ACTION)
  const start = useCallback((request: ActionRequest) => setAction((current) => nextAction(current, request)), [])
  const result = useQuery(actionQueryOptions(action))
  return { action, start, result }
}
