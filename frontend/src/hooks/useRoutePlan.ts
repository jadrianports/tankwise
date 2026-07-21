import { useCallback, useRef, useState } from 'react';

import { planRoute } from '../api/routeClient';
import { HERO_VEHICLE_PRESET_ID, VEHICLE_PRESETS } from '../constants/presets';
import type { RouteResponse, VehicleProfileRequest } from '../types/routeContract';

// The app loads with the hero preset (Semi loaded) selected and sends it
// explicitly -- the backend's own default (10 mpg / 500 mi) is unchanged
// for any request that omits `vehicle` entirely, but every request this
// hook issues always carries one.
const HERO_VEHICLE = VEHICLE_PRESETS.find((preset) => preset.id === HERO_VEHICLE_PRESET_ID)!.vehicle;

// `rate_limited` is a distinct status (not `error`) so a 429 never trips
// ResultsSection's error-alert branch or clears the last good `data` --
// the previous plan must stay fully visible while a 429 cooldown counts
// down.
export type RoutePlanStatus = 'idle' | 'loading' | 'success' | 'error' | 'rate_limited';

export interface RoutePlanError {
  code: string;
  message: string;
  retryAfterS?: number;
}

export interface UseRoutePlanResult {
  status: RoutePlanStatus;
  data: RouteResponse | null;
  error: RoutePlanError | null;
  // `vehicle` is an optional one-shot override for the very first submit of
  // a session -- e.g. a share-URL auto-solve, which must send
  // the ENCODED vehicle profile on its one and only request rather than the
  // hero default `vehicleRef` starts with. Every other caller (the planner
  // form, demo chips, recent trips) omits it and gets the currently
  // selected vehicle, unchanged.
  submit: (start: string, finish: string, vehicle?: VehicleProfileRequest) => Promise<void>;
  retry: () => void;
  // Updates the vehicle profile used by every future submit/retry, and --
  // if a route has already been solved at least once -- immediately
  // re-solves using the last-submitted (already-resolved) start/finish
  // coordinates. Never re-geocodes: a slider/chip change only ever
  // reuses cached coordinates, it never touches AddressAutocomplete.
  resolveVehicle: (vehicle: VehicleProfileRequest) => void;
}

function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError';
}

// Submit state machine: idle -> loading -> (success | error). Plain
// useState/useCallback is sufficient here -- no external query library needed
// for a single in-flight request per submit.
//
// A `useRef` sequence counter and an `AbortController` per call. A second
// submit aborts the first outright (so the browser actually cancels the
// in-flight request, not just ignores its result), and every `setState`
// after the `await` is gated on the captured sequence number still
// matching the ref's current value -- a stale response can never
// overwrite a newer one, which is exactly the bug the debounced what-if
// sliders would otherwise turn from latent into reproducible.
export function useRoutePlan(): UseRoutePlanResult {
  const [status, setStatus] = useState<RoutePlanStatus>('idle');
  const [data, setData] = useState<RouteResponse | null>(null);
  const [error, setError] = useState<RoutePlanError | null>(null);

  const sequenceRef = useRef(0);
  const controllerRef = useRef<AbortController | null>(null);
  // Last-submitted (start, finish) pair -- lets an `upstream_error` state
  // offer a real Retry button that resubmits the same request,
  // without ResultsSection needing to know the last-entered coordinates
  // itself. Also what `resolveVehicle` reuses so a slider/chip change
  // never re-geocodes.
  const lastArgsRef = useRef<{ start: string; finish: string } | null>(null);
  // Current vehicle profile, sent on every submit (starting from the
  // hero default, updated live by `resolveVehicle` as the user picks a
  // preset/drags a slider). A `useRef` (not `useState`) because it's read
  // inside `submit` without needing its own re-render -- VehicleSection
  // owns the visible slider/chip UI state independently.
  const vehicleRef = useRef<VehicleProfileRequest>(HERO_VEHICLE);

  const submit = useCallback(async (start: string, finish: string, vehicle?: VehicleProfileRequest) => {
    if (vehicle) vehicleRef.current = vehicle;
    lastArgsRef.current = { start, finish };
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;

    const mySequence = ++sequenceRef.current;

    setStatus('loading');
    setError(null);

    let result;
    try {
      result = await planRoute(start, finish, vehicleRef.current, controller.signal);
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
      setError(null);
    } else if (result.code === 'rate_limited') {
      // A 429 must never blank the last good plan or read as a
      // solver failure -- `data` is deliberately left untouched, and the
      // distinct `rate_limited` status keeps ResultsSection's
      // `status === 'error'` alert branch from firing.
      setError({ code: result.code, message: result.message, retryAfterS: result.retryAfterS });
      setStatus('rate_limited');
    } else {
      setData(null);
      setError({ code: result.code, message: result.message });
      setStatus('error');
    }
  }, []);

  const retry = useCallback(() => {
    const last = lastArgsRef.current;
    if (!last) return;
    void submit(last.start, last.finish);
  }, [submit]);

  const resolveVehicle = useCallback(
    (vehicle: VehicleProfileRequest) => {
      vehicleRef.current = vehicle;
      const last = lastArgsRef.current;
      if (!last) return; // no route solved yet -- nothing to re-solve against
      void submit(last.start, last.finish);
    },
    [submit]
  );

  return { status, data, error, submit, retry, resolveVehicle };
}
