// Typed fetch client for GET /api/config, mirroring routeClient.ts's
// planRoute try/catch-network -> parse-body -> ok/error shape exactly.
// Fetched once at boot to get the browser-facing pk. Mapbox token.
import type { ConfigResponse } from '../types/routeContract';

export interface FetchConfigSuccess {
  ok: true;
  data: ConfigResponse;
}

export interface FetchConfigFailure {
  ok: false;
  code: 'config_error';
}

export type FetchConfigResult = FetchConfigSuccess | FetchConfigFailure;

// Every failure mode -- network error, non-2xx, malformed body, or a body
// that parses but is missing mapbox_public_token -- collapses to the same
// `config_error` pseudo-code. The caller (App.tsx/MapView.tsx)
// never needs to branch on *why* the token isn't available, only that it
// isn't; `config_error` is the same pseudo-code routeClient.ts's
// mapErrorToMessage already handles (see its docstring -- the backend's
// own misconfiguration failure mode reuses the generic upstream_error
// code, which this client remaps here rather than at the call site).
export async function fetchConfig(): Promise<FetchConfigResult> {
  let res: Response;
  try {
    res = await fetch('/api/config');
  } catch {
    return { ok: false, code: 'config_error' };
  }

  const body = await res.json().catch(() => null);

  if (!res.ok || !body || typeof body.mapbox_public_token !== 'string' || !body.mapbox_public_token) {
    return { ok: false, code: 'config_error' };
  }

  return { ok: true, data: body as ConfigResponse };
}
