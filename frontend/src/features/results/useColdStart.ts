import { useEffect, useState, useSyncExternalStore } from 'react';

import { getBootSignalSnapshot, subscribeBootSignal } from '../../api/routeClient';

export type ColdStartStage = 'solving' | 'checking' | 'waking' | 'pricing';

// "A few seconds" before the narration admits it's taking a while, and a
// longer threshold before it escalates further. 4000ms is D-07's original
// threshold for escalating the presentation -- unchanged here. What DOES
// change at that threshold is which of two honest stages it escalates to:
// `waking` only when routeClient's boot-retry loop or readyClient's
// pre-warm ping has recorded a genuine "server did not answer" signal
// (see `../../api/routeClient`'s `subscribeBootSignal`/
// `getBootSignalSnapshot`); `pricing` -- an equally honest "still
// genuinely computing" stage -- for a request that is merely slow but
// answering. A cold start must never read as a hung request, AND a slow
// warm request must never read as a cold start -- the narration escalates
// instead of sitting on one static string, but the escalation is truthful
// either way.
const CHECKING_THRESHOLD_MS = 3000;
const WAKING_THRESHOLD_MS = 4000;

// Internal timer-only state: purely elapsed-time driven, exactly as
// before. The externally-visible `ColdStartStage` is derived from this
// PLUS the reactive boot signal below -- see the return statement.
type _ElapsedStage = 'solving' | 'checking' | 'past-threshold';

export function useColdStart(isLoading: boolean): ColdStartStage {
  const [elapsedStage, setElapsedStage] = useState<_ElapsedStage>('solving');
  // Reactive read of the shared not-answering signal -- a transition from
  // false -> true while already past the threshold immediately re-renders
  // from `pricing` to `waking`, with no new timer of its own.
  const bootSignalFired = useSyncExternalStore(subscribeBootSignal, getBootSignalSnapshot);

  useEffect(() => {
    if (!isLoading) {
      setElapsedStage('solving');
      return;
    }

    const checkingTimer = setTimeout(() => setElapsedStage('checking'), CHECKING_THRESHOLD_MS);
    const escalateTimer = setTimeout(() => setElapsedStage('past-threshold'), WAKING_THRESHOLD_MS);

    return () => {
      clearTimeout(checkingTimer);
      clearTimeout(escalateTimer);
    };
  }, [isLoading]);

  if (elapsedStage === 'past-threshold') {
    return bootSignalFired ? 'waking' : 'pricing';
  }
  return elapsedStage;
}
