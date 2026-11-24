import { Badge } from '@/components/ui/badge'
import type { BadgeState } from '@/lib/badge'
import { cn } from '@/lib/utils'

const TONES = {
  live: 'bg-green-100 text-green-800',
  replay: 'bg-indigo-100 text-indigo-800',
  stale: 'bg-red-100 text-red-800',
}

export function ModeBadge({ badge, otpDown }: { badge: BadgeState; otpDown: boolean }) {
  return (
    <div className="flex flex-wrap items-center gap-2.5" aria-live="polite">
      <Badge
        id="mode-badge"
        data-mode={badge.mode}
        data-stale={badge.stale}
        className={cn('h-6 gap-1.5 px-2.5 font-bold tracking-wide lowercase', TONES[badge.stale ? 'stale' : badge.mode])}
      >
        <span className="size-1.5 rounded-full bg-current" aria-hidden="true" />
        {badge.mode}
      </Badge>
      <span id="feed-age-text" className="text-xs text-slate-300 tabular-nums">
        {badge.text}
      </span>
      {otpDown && (
        <Badge id="otp-status" variant="destructive" className="bg-red-100">
          OTP down
        </Badge>
      )}
    </div>
  )
}
