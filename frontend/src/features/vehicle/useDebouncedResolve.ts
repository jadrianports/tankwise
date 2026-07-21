import { useCallback, useEffect, useRef, useState } from 'react';

import { useRoutePlanContext } from '../../context/RoutePlanContext';
import type { VehicleProfileRequest } from '../../types/routeContract';

// A ten-second drag gesture must cost exactly one request, comfortably
// inside the backend's ~20/min throttle burst.
const DEBOUNCE_MS = 500;

export interface UseDebouncedResolveResult {
  // Called on every preset-chip click and every slider tick. Coalesces a
  // whole drag into one re-solve; never called directly against
  // `resolveVehicle` itself so VehicleSection never needs to know about
  // debouncing or the 429 cooldown.
  onVehicleChange: (vehicle: VehicleProfileRequest) => void;
  isPaused: boolean;
  retryCountdown: number | null;
}

// Wraps the hardened `useRoutePlan` submit (via RoutePlanContext's
// `resolveVehicle`) with a ~500ms debounce and a 429 pause/countdown/
// self-resume. `resolveVehicle` itself already reuses the last-resolved
// (start, finish) pair cached inside `useRoutePlan` -- this hook never
// touches coordinates directly, so a slider/chip change can never
// trigger a re-geocode. The camera holding position is unaffected:
// MapView only refits on start/finish change, and this hook never
// changes those.
export function useDebouncedResolve(): UseDebouncedResolveResult {
  const { status, error, resolveVehicle, retry } = useRoutePlanContext();

  const debounceTimerRef = useRef<number | null>(null);
  // The most recent vehicle value the user actually wants applied --
  // recorded even while paused so the exact latest slider position (not
  // an earlier, already-throttled one) is what fires the moment the 429
  // cooldown clears.
  const pendingRef = useRef<VehicleProfileRequest | null>(null);
  const [retryCountdown, setRetryCountdown] = useState<number | null>(null);
  const isPaused = retryCountdown !== null;

  const flush = useCallback(() => {
    const pending = pendingRef.current;
    pendingRef.current = null;
    if (pending) {
      resolveVehicle(pending);
    }
  }, [resolveVehicle]);

  const onVehicleChange = useCallback(
    (vehicle: VehicleProfileRequest) => {
      pendingRef.current = vehicle;
      if (isPaused) return; // A 429 suspends slider-triggered requests entirely
      if (debounceTimerRef.current !== null) {
        window.clearTimeout(debounceTimerRef.current);
      }
      debounceTimerRef.current = window.setTimeout(flush, DEBOUNCE_MS);
    },
    [flush, isPaused]
  );

  // A fresh `rate_limited` response starts (or restarts) the countdown
  // from the backend-supplied `retry_after_s` -- never invented client-side.
  useEffect(() => {
    if (status !== 'rate_limited' || typeof error?.retryAfterS !== 'number') return;
    setRetryCountdown(Math.max(1, Math.ceil(error.retryAfterS)));
  }, [status, error]);

  // Ticks the countdown down once a second; at zero it self-resumes:
  // applies whatever vehicle value the user last landed on during the
  // cooldown, or -- if nothing changed while paused -- retries the exact
  // request the throttle rejected. Both paths reuse cached coordinates,
  // never re-geocoding.
  useEffect(() => {
    if (retryCountdown === null) return;
    if (retryCountdown <= 0) {
      setRetryCountdown(null);
      if (pendingRef.current) {
        flush();
      } else {
        retry();
      }
      return;
    }
    const id = window.setTimeout(() => {
      setRetryCountdown((current) => (current === null ? null : current - 1));
    }, 1000);
    return () => window.clearTimeout(id);
  }, [retryCountdown, flush, retry]);

  useEffect(() => {
    return () => {
      if (debounceTimerRef.current !== null) {
        window.clearTimeout(debounceTimerRef.current);
      }
    };
  }, []);

  return { onVehicleChange, isPaused, retryCountdown };
}
