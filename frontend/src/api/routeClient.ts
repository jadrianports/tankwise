// Typed fetch client for POST /api/route, plus a pure per-error-code envelope
// mapper. The envelope shape and every code/detail
// key here are grounded directly in routing/exceptions.py -- not guessed.
import type { InfeasibleRouteDetail, RouteResponse, VehicleProfileRequest } from '../types/routeContract';

const GENERIC_FALLBACK_MESSAGE = 'Something went wrong. Please try again.';

export interface ApiErrorDetail {
  [key: string]: unknown;
}

export interface ApiError {
  code: string;
  message?: string;
  detail?: ApiErrorDetail;
}

// Pure function: maps a parsed `{code, message, detail}` error object to the
// exact user-facing copy. Kept
// independently importable/testable without touching `fetch`.
//
// `rate_limited` and `config_error` are added cases in this
// same switch, not a second error-mapping function -- error-copy logic
// stays in one auditable place. `config_error` is a client-assigned
// pseudo-code: the backend's own failure mode for a misconfigured token is
// `upstream_error` (routing/exceptions.py's ImproperlyConfigured branch),
// the same code POST /api/route uses for a Mapbox outage -- the eventual
// GET /api/config caller must remap that response to `config_error` before
// calling this function, so the two situations never collide on one
// message.
export function mapErrorToMessage(error: ApiError | null | undefined): string {
  if (!error || typeof error !== 'object') {
    return GENERIC_FALLBACK_MESSAGE;
  }

  const { code, message, detail } = error;

  switch (code) {
    case 'invalid_input': {
      // DRF-wrapped ValidationError nests the field message in `detail`
      // (e.g. {"start": ["Coordinate (...) is outside..."]}); InvalidRouteInputError
      // instead carries its message directly with an empty `detail` -- fall back to `message`.
      if (detail && typeof detail === 'object' && Object.keys(detail).length > 0) {
        return Object.values(detail).flat().join(' ');
      }
      return message || GENERIC_FALLBACK_MESSAGE;
    }
    case 'infeasible_route': {
      // `leg_index`/`leg_coords` are additive (D-07/WAY-05) and unused in
      // this message today -- the named-leg callout (D-08) is downstream
      // UI work (13-05). Destructuring via the full `InfeasibleRouteDetail`
      // type keeps this call site in sync with the envelope's real shape.
      const { max_range_mi, from_station, to_station, gap_mi } = (detail ?? {}) as Partial<InfeasibleRouteDetail>;
      return `No fuel stop reachable within ${max_range_mi} mi between ${from_station} and ${to_station} (gap: ${gap_mi} mi).`;
    }
    case 'route_not_found':
      return 'No drivable route between these points.';
    case 'upstream_error':
      return 'Map service unavailable. Please retry.';
    case 'rate_limited': {
      // The backend's Throttled.wait supplies retry_after_s; framed as
      // catching-up, never as a solver failure.
      const { retry_after_s } = (detail ?? {}) as { retry_after_s?: number };
      return `Catching up — retrying in ${retry_after_s ?? '…'}s`;
    }
    case 'config_error':
      return 'Map unavailable — the interactive map needs a valid Mapbox token. The route planner below still works.';
    default:
      return GENERIC_FALLBACK_MESSAGE;
  }
}

export interface PlanRouteSuccess {
  ok: true;
  data: RouteResponse;
}

export interface PlanRouteFailure {
  ok: false;
  code: string;
  message: string;
  // Only populated for a `rate_limited` (429) failure -- the raw seconds
  // from `error.detail.retry_after_s`, kept as a number (not re-parsed
  // from the already-composed message string) so a caller can drive an
  // actual countdown timer.
  retryAfterS?: number;
  // Additive (D-08/WAY-05, Phase 13): the raw `error.detail` envelope,
  // passed through unparsed so a caller can read `leg_index`/`leg_coords`
  // (InfeasibleRouteDetail) for the named-leg infeasible callout without
  // this module needing to know about every possible error code's own
  // detail shape.
  detail?: ApiErrorDetail;
}

export type PlanRouteResult = PlanRouteSuccess | PlanRouteFailure;

function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError';
}

// POSTs the relative /api/route path (identical in dev via the Vite proxy and
// in Docker via the WhiteNoise-served single service -- see vite.config.ts).
//
// `signal` is threaded straight into `fetch` so a caller (useRoutePlan)
// can cancel an in-flight request when a newer submit supersedes it. An
// aborted fetch's `AbortError` is rethrown, not swallowed here or mapped
// through the generic network-error branch -- an intentional cancellation
// must never surface as a user-facing error; only the caller (which knows
// whether the abort was itself vs. a stale race) can safely decide to
// ignore it.
// `waypoints` is optional and additive (WAY-03) -- omitted or empty, the
// request body is the byte-identical `{start, finish[, vehicle]}` shape
// this endpoint has always sent. `vehicle` is optional -- omitting it
// lets the backend fall back to its own documented default (10 mpg / 500
// mi / full tank). Every preset-chip/slider-driven call passes it
// explicitly so the hero preset wins in the UI without changing that API
// default.
export async function planRoute(
  start: string,
  finish: string,
  waypoints?: string[],
  vehicle?: VehicleProfileRequest | null,
  signal?: AbortSignal
): Promise<PlanRouteResult> {
  const requestBody: { start: string; finish: string; waypoints?: string[]; vehicle?: VehicleProfileRequest } = {
    start,
    finish,
  };
  if (waypoints && waypoints.length > 0) {
    requestBody.waypoints = waypoints;
  }
  if (vehicle) {
    requestBody.vehicle = vehicle;
  }

  let res: Response;
  try {
    res = await fetch('/api/route', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(requestBody),
      signal,
    });
  } catch (err) {
    if (isAbortError(err)) {
      throw err;
    }
    return { ok: false, code: 'network_error', message: GENERIC_FALLBACK_MESSAGE };
  }

  const body = await res.json().catch(() => null);

  if (res.ok) {
    return { ok: true, data: body as RouteResponse };
  }

  if (!body || !body.error) {
    return { ok: false, code: 'network_error', message: GENERIC_FALLBACK_MESSAGE };
  }

  const { code, message, detail } = body.error;
  const retryAfterS =
    typeof (detail as { retry_after_s?: unknown })?.retry_after_s === 'number'
      ? (detail as { retry_after_s: number }).retry_after_s
      : undefined;
  return { ok: false, code, message: mapErrorToMessage({ code, message, detail }), retryAfterS, detail };
}
