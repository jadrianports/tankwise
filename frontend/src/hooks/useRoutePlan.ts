import { useCallback, useRef, useState } from 'react';

import { planRoute } from '../api/routeClient';
import type { RouteResponse } from '../types/routeContract';

export type RoutePlanStatus = 'idle' | 'loading' | 'success' | 'error';

export interface RoutePlanError {
  code: string;
  message: string;
}

export interface UseRoutePlanResult {
  status: RoutePlanStatus;
  data: RouteResponse | null;
  error: RoutePlanError | null;
  submit: (start: string, finish: string) => Promise<void>;
}

function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError';
}

// Submit state machine: idle -> loading -> (success | error). Plain
// useState/useCallback is sufficient here -- no external query library needed
// for a single in-flight request per submit.
//
// D-04 (mandatory): a `useRef` sequence counter and an `AbortController`
// per call. A second submit aborts the first outright (so the browser
// actually cancels the in-flight request, not just ignores its result),
// and every `setState` after the `await` is gated on the captured sequence
// number still matching the ref's current value -- a stale response can
// never overwrite a newer one, which is exactly the bug D-14's debounced
// re-solve would otherwise turn from latent into reproducible.
export function useRoutePlan(): UseRoutePlanResult {
  const [status, setStatus] = useState<RoutePlanStatus>('idle');
  const [data, setData] = useState<RouteResponse | null>(null);
  const [error, setError] = useState<RoutePlanError | null>(null);

  const sequenceRef = useRef(0);
  const controllerRef = useRef<AbortController | null>(null);

  const submit = useCallback(async (start: string, finish: string) => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;

    const mySequence = ++sequenceRef.current;

    setStatus('loading');
    setError(null);

    let result;
    try {
      result = await planRoute(start, finish, controller.signal);
    } catch (err) {
      if (isAbortError(err)) {
        // Intentional cancellation -- a newer submit superseded this one.
        // Never surfaces as a user-facing error.
        return;
      }
      throw err;
    }

    // A newer submit already started (and may have already resolved) since
    // this call began -- this response is stale and must not overwrite
    // state a later call already owns.
    if (mySequence !== sequenceRef.current) {
      return;
    }

    if (result.ok) {
      setData(result.data);
      setStatus('success');
    } else {
      setData(null);
      setError({ code: result.code, message: result.message });
      setStatus('error');
    }
  }, []);

  return { status, data, error, submit };
}
