import { useEffect, useState, useSyncExternalStore } from 'react';

import { getBootSignalSnapshot, subscribeBootSignal } from '../../api/routeClient';

export type ColdStartStage = 'solving' | 'checking' | 'working' | 'waking';

// "A few seconds" before the narration admits it's taking a while, then two
// separate thresholds that can each escalate the stage to `waking`.
//
// ESCALATE_THRESHOLD_MS (4000ms, D-07's original figure) is where the FAST
// path becomes eligible: once routeClient's boot-retry loop or
// readyClient's pre-warm ping has already recorded a genuine "server did
// not answer" signal (see `../../api/routeClient`'s `subscribeBootSignal`/
// `getBootSignalSnapshot`), the stage resolves straight to `waking` here.
//
// WAKING_ELAPSED_THRESHOLD_MS (18000ms) is a second, elapsed-time-only path
// to the same `waking` stage, for a request that never errors at all.
// Render's free-tier proxy holds the connection open while a sleeping
// instance wakes -- the request neither rejects nor returns a 5xx, it
// simply takes roughly 50 seconds and then returns 200 -- so a
// failure-gated signal can never fire for it. 18000 sits comfortably above
// every measured warm figure and comfortably below the measured cold one:
// a genuine cache-miss solve of the slowest demo route measured 10.8s wall
// (plus up to ~400ms for a cold worker's one-time station-index build --
// see the `warm` job's comment in `.github/workflows/keep-warm.yml`),
// while a real hold-open wake was observed to take 49.8s live. A slow warm
// request must never read as a cold start -- the 18s figure sitting well
// above the ~11.2s warm ceiling is what guarantees that.
//
// Below WAKING_ELAPSED_THRESHOLD_MS the client genuinely cannot tell a
// sleeping instance from an expensive corridor apart, so the `working`
// stage (see LoadingNarration.tsx) names neither one as fact.
const CHECKING_THRESHOLD_MS = 3000;
const ESCALATE_THRESHOLD_MS = 4000;
const WAKING_ELAPSED_THRESHOLD_MS = 18000;

// Internal timer-only state: purely elapsed-time driven. The externally
// visible `ColdStartStage` is derived from this PLUS the reactive boot
// signal below -- see the return statement.
type _ElapsedStage = 'solving' | 'checking' | 'escalated' | 'long-wait';

export function useColdStart(isLoading: boolean): ColdStartStage {
  const [elapsedStage, setElapsedStage] = useState<_ElapsedStage>('solving');
  // Reactive read of the shared not-answering signal -- a transition from
  // false -> true while already past the escalate threshold immediately
  // re-renders from `working` to `waking`, with no new timer of its own.
  const bootSignalFired = useSyncExternalStore(subscribeBootSignal, getBootSignalSnapshot);

  useEffect(() => {
    if (!isLoading) {
      setElapsedStage('solving');
      return;
    }

    const checkingTimer = setTimeout(() => setElapsedStage('checking'), CHECKING_THRESHOLD_MS);
    const escalateTimer = setTimeout(() => setElapsedStage('escalated'), ESCALATE_THRESHOLD_MS);
    const longWaitTimer = setTimeout(
      () => setElapsedStage('long-wait'),
      WAKING_ELAPSED_THRESHOLD_MS
    );

    return () => {
      clearTimeout(checkingTimer);
      clearTimeout(escalateTimer);
      clearTimeout(longWaitTimer);
    };
  }, [isLoading]);

  if (elapsedStage === 'long-wait') {
    return 'waking';
  }
  if (elapsedStage === 'escalated') {
    return bootSignalFired ? 'waking' : 'working';
  }
  return elapsedStage;
}
