import { useEffect, useState } from 'react';

export type ColdStartStage = 'solving' | 'checking' | 'waking';

// "A few seconds" before the narration admits it's taking a while, and a
// longer threshold before it names the free-tier cold-start possibility.
// 4000ms is D-07's threshold for escalating to the waking presentation --
// the same threshold now drives two consumers: the existing inline
// LoadingNarration copy and the dedicated ColdStartModal overlay.
const CHECKING_THRESHOLD_MS = 3000;
const WAKING_THRESHOLD_MS = 4000;

// Cold start is detected purely by an elapsed-time threshold on the
// in-flight request -- no extra request, no /api/health probe, no server
// signal. This hook is nothing more than two timers keyed off `isLoading`;
// it never calls fetch. A cold start must never read as a hung request, so
// the narration escalates instead of sitting on one static string.
export function useColdStart(isLoading: boolean): ColdStartStage {
  const [stage, setStage] = useState<ColdStartStage>('solving');

  useEffect(() => {
    if (!isLoading) {
      setStage('solving');
      return;
    }

    const checkingTimer = setTimeout(() => setStage('checking'), CHECKING_THRESHOLD_MS);
    const wakingTimer = setTimeout(() => setStage('waking'), WAKING_THRESHOLD_MS);

    return () => {
      clearTimeout(checkingTimer);
      clearTimeout(wakingTimer);
    };
  }, [isLoading]);

  return stage;
}
