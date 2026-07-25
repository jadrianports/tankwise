import { markServerNotAnswering } from './routeClient';

// Fire-and-forget pre-warm ping for a sleeping free-tier dyno (D-07). Reuses
// the existing `GET /api/ready` health-gate endpoint (routing/views.py's
// ReadyView, already unthrottled) rather than adding a new one -- its own
// response body is irrelevant here; the only effect this module cares
// about is the act of issuing the request, which begins waking the dyno
// while the user is still reading or typing on the planner form. The
// result body is still deliberately discarded and never surfaced to the
// UI -- but a non-OK response or a rejected fetch IS recorded, additively,
// into routeClient's shared not-answering signal as a second, independent
// source alongside `planRoute`'s own boot-retry loop. This ping's promise
// can easily still be pending (or resolve late) well after the user has
// already submitted the form, so its eventual failure is a genuine signal
// even mid-request.
export async function prewarmServer(): Promise<void> {
  const response = await fetch('/api/ready').catch(() => null);
  if (!response || !response.ok) {
    markServerNotAnswering();
  }
}
