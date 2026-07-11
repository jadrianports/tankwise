// Typed fetch client for POST /api/route, plus a pure per-error-code envelope
// mapper (05-UI-SPEC State Contract). The envelope shape and every code/detail
// key here are grounded directly in routing/exceptions.py -- not guessed.
const GENERIC_FALLBACK_MESSAGE = 'Something went wrong. Please try again.';

// Pure function: maps a parsed `{code, message, detail}` error object to the
// exact user-facing copy from the 05-UI-SPEC State Contract. Kept
// independently importable/testable without touching `fetch`.
export function mapErrorToMessage(error) {
  if (!error || typeof error !== 'object') {
    return GENERIC_FALLBACK_MESSAGE;
  }

  const { code, message, detail } = error;

  switch (code) {
    case 'invalid_input': {
      // DRF-wrapped ValidationError nests the real field message one level
      // deeper in `detail` (e.g. {"start": ["Coordinate (...) is outside..."]});
      // InvalidRouteInputError instead carries its specific message directly
      // with an empty `detail` -- fall back to `message` in that case.
      if (detail && typeof detail === 'object' && Object.keys(detail).length > 0) {
        return Object.values(detail).flat().join(' ');
      }
      return message || GENERIC_FALLBACK_MESSAGE;
    }
    case 'infeasible_route': {
      const { max_range_mi, from_station, to_station, gap_mi } = detail || {};
      return `No fuel stop reachable within ${max_range_mi} mi between ${from_station} and ${to_station} (gap: ${gap_mi} mi).`;
    }
    case 'route_not_found':
      return 'No drivable route between these points.';
    case 'upstream_error':
      return 'Map service unavailable — please retry.';
    default:
      return GENERIC_FALLBACK_MESSAGE;
  }
}

// POSTs the relative /api/route path (identical in dev via the Vite proxy and
// in Docker via the Nginx reverse proxy -- see vite.config.js).
export async function planRoute(start, finish) {
  let res;
  try {
    res = await fetch('/api/route', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ start, finish }),
    });
  } catch {
    return { ok: false, code: 'network_error', message: GENERIC_FALLBACK_MESSAGE };
  }

  const body = await res.json().catch(() => null);

  if (res.ok) {
    return { ok: true, data: body };
  }

  if (!body || !body.error) {
    return { ok: false, code: 'network_error', message: GENERIC_FALLBACK_MESSAGE };
  }

  const { code, message, detail } = body.error;
  return { ok: false, code, message: mapErrorToMessage({ code, message, detail }) };
}
