import type { JourneyResponse } from '@/api/types'
import { Button } from '@/components/ui/button'
import { fmtAge } from '@/lib/format'

function abstentionDetails(response: JourneyResponse): string {
  const abstention = response.abstention
  const details: string[] = []
  if (abstention?.code) details.push(`Code: ${abstention.code}`)
  if (abstention?.feed_age_seconds != null) details.push(`Feed age: ${fmtAge(abstention.feed_age_seconds)}`)
  if (abstention?.threshold_seconds != null) details.push(`Freshness threshold: ${fmtAge(abstention.threshold_seconds)}`)
  if (abstention?.last_feed_timestamp) details.push(`Last GPS timestamp: ${abstention.last_feed_timestamp}`)
  if (response.feed_summary?.paused) details.push('Poller state: paused')
  return details.join(' · ') || 'Options suppressed until timing data is sufficient.'
}

interface AbstentionPanelProps {
  response: JourneyResponse
  onRetry: () => void
  onUseReplay: () => void
}

export function AbstentionPanel({ response, onRetry, onUseReplay }: AbstentionPanelProps) {
  return (
    <section
      id="abstention-panel"
      role="alert"
      aria-live="assertive"
      data-code={response.abstention?.code}
      className="rounded-xl border-2 border-red-300 bg-red-100 p-3.5 text-red-800"
    >
      <h2 className="mb-1.5 text-base font-extrabold">Not enough data</h2>
      <p id="abstention-reason" className="my-1 text-[13px]">
        {response.abstention?.reason || 'Not enough data: live GPS feed is stale or unavailable.'}
      </p>
      <div id="abstention-meta" className="mt-2 rounded-md bg-white/70 px-2.5 py-2 text-xs text-red-900">
        {abstentionDetails(response)}
      </div>
      <div className="mt-2.5 flex gap-2">
        <Button id="abstention-retry-btn" type="button" variant="outline" size="sm" onClick={onRetry}>
          Retry live query
        </Button>
        <Button id="abstention-replay-btn" type="button" variant="outline" size="sm" onClick={onUseReplay}>
          Use October replay
        </Button>
      </div>
    </section>
  )
}
