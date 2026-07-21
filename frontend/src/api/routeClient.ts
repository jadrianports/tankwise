// Typed fetch client for POST /api/route, plus a pure per-error-code envelope
// mapper. The envelope shape and every code/detail
// key here are grounded directly in routing/exceptions.py -- not guessed.
import type { RouteResponse } from '../types/routeContract';

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
// `rate_limited` (D-17) and `config_error` (D-08) are added cases in this
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
      const { max_range_mi, from_station, to_station, gap_mi } = (detail ?? {}) as {
        max_range_mi?: string;
        from_station?: string;
        to_station?: string;
        gap_mi?: string;
      };
      return `No fuel stop reachable within ${max_range_mi} mi between ${from_station} and ${to_station} (gap: ${gap_mi} mi).`;
    }
    case 'route_not_found':
      return 'No drivable route between these points.';
    case 'upstream_error':
      return 'Map service unavailable. Please retry.';
    case 'rate_limited': {
      // Phase 8 D-15 supplies retry_after_s via Throttled.wait; framed as
      // catching-up, never as a solver failure (D-17).
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
}

export type PlanRouteResult = PlanRouteSuccess | PlanRouteFailure;

function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError';
}

// POSTs the relative /api/route path (identical in dev via the Vite proxy and
// in Docker via the WhiteNoise-served single service -- see vite.config.ts).
//
// `signal` (D-04) is threaded straight into `fetch` so a caller (useRoutePlan)
// can cancel an in-flight request when a newer submit supersedes it. An
// aborted fetch's `AbortError` is rethrown, not swallowed here or mapped
// through the generic network-error branch -- an intentional cancellation
// must never surface as a user-facing error; only the caller (which knows
// whether the abort was itself vs. a stale race) can safely decide to
// ignore it.
export async function planRoute(
  start: string,
  finish: string,
  signal?: AbortSignal
): Promise<PlanRouteResult> {
  let res: Response;
  try {
    res = await fetch('/api/route', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ start, finish }),
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
  return { ok: false, code, message: mapErrorToMessage({ code, message, detail }) };
}
