// Fire-and-forget pre-warm ping for a sleeping free-tier dyno (D-07). Reuses
// the existing `GET /api/ready` health-gate endpoint (routing/views.py's
// ReadyView, already unthrottled) rather than adding a new one -- its own
// response body is irrelevant here; the only effect this module cares
// about is the act of issuing the request, which begins waking the dyno
// while the user is still reading or typing on the planner form. The
// result -- 200, 503, or a rejected network promise -- is deliberately
// discarded and never surfaced to the UI.
export async function prewarmServer(): Promise<void> {
  await fetch('/api/ready').catch(() => {});
}
