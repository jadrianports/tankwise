// The single canonical trip-state serialization shape. Reused unchanged
// by useRecentTrips.ts and by the share URL below -- never fork a second
// shape.
//
// `start`/`finish` are the RESOLVED values already sent to POST /api/route
// (a "lat,lng" string once an autocomplete suggestion resolves, or the raw
// typed string as a fallback); `startLabel`/`finishLabel` are the
// human-readable display labels, kept client-side only. `vehicle` is a
// VehiclePreset id (constants/presets.ts) -- lean enough for both a query
// string and localStorage, and the single source of truth other
// vehicle-preset consumers can read/write without this shape changing.
import { HERO_VEHICLE_PRESET_ID } from '../../constants/presets';

export interface TripState {
  start: string;
  finish: string;
  startLabel: string;
  finishLabel: string;
  vehicle: string;
  // Additive (D-10/WAY-07): order-preserving list of intermediate
  // waypoint values, same "lat,lng"/address string shape as start/finish.
  // Empty/absent for a plain A->B trip -- old share links and recent
  // trips keep decoding exactly as before.
  waypoints?: string[];
  // Additive (D-11): the trip's total expected stop count (start + every
  // waypoint + finish), as ENCODED by the originating client -- always
  // `2 + waypoints.length` when `encodeTripState` writes it. Compared at
  // decode time against what THIS decode actually recovered to detect a
  // stale client that can't fully parse `waypoints` -- see
  // `staleClientWarning`.
  stopCount?: number;
  // Additive (D-11): set only when a decoded `stopCount` hint exceeds
  // what this decode actually recovered (start + finish + parsed
  // waypoints) -- signals a stale client degrading a multi-stop link,
  // instead of silently collapsing it to a 2-point A->B trip.
  staleClientWarning?: string;
}

const PARAM_KEYS = {
  start: 'start',
  finish: 'finish',
  startLabel: 'from',
  finishLabel: 'to',
  vehicle: 'vehicle',
  waypoints: 'stops',
  stopCount: 'stopCount',
} as const;

// Waypoint values (each already a "lat,lng"/address string, which itself
// contains commas) are joined with `;` -- distinct from the comma used
// inside each individual value, so the joined list splits back apart
// unambiguously.
const WAYPOINTS_SEPARATOR = ';';

// Readable query params -- e.g.
// `?start=34.05%2C-118.24&finish=40.71%2C-74.01&from=Los+Angeles&to=New+York&vehicle=semi-loaded`,
// short enough to paste anywhere and legible in a browser address bar.
// `stops`/`stopCount` are written together, only when waypoints exist --
// an A->B trip's encoded URL is byte-for-byte unchanged from before D-10.
export function encodeTripState(state: TripState): URLSearchParams {
  const params = new URLSearchParams();
  params.set(PARAM_KEYS.start, state.start);
  params.set(PARAM_KEYS.finish, state.finish);
  if (state.startLabel) params.set(PARAM_KEYS.startLabel, state.startLabel);
  if (state.finishLabel) params.set(PARAM_KEYS.finishLabel, state.finishLabel);
  params.set(PARAM_KEYS.vehicle, state.vehicle);
  if (state.waypoints && state.waypoints.length > 0) {
    params.set(PARAM_KEYS.waypoints, state.waypoints.join(WAYPOINTS_SEPARATOR));
    // Always derived fresh from the waypoints actually being encoded --
    // never trusts a caller-supplied `state.stopCount` -- so the hint
    // can never itself be wrong at the point of encoding.
    params.set(PARAM_KEYS.stopCount, String(2 + state.waypoints.length));
  }
  return params;
}

export function tripStateToQueryString(state: TripState): string {
  return encodeTripState(state).toString();
}

// Returns null when the minimum required params (start/finish) are
// missing -- a malformed or partial query string is not a valid trip.
// `start`/`finish` stay hard-required, UNCHANGED by D-10/D-11 (Pitfall
// 14) -- only the new `stops`/`stopCount` params are optional and
// independently defaulted.
export function decodeTripState(search: string | URLSearchParams): TripState | null {
  const params = typeof search === 'string' ? new URLSearchParams(search) : search;
  const start = params.get(PARAM_KEYS.start);
  const finish = params.get(PARAM_KEYS.finish);
  if (!start || !finish) return null;

  const rawWaypoints = params.get(PARAM_KEYS.waypoints);
  const waypoints = rawWaypoints
    ? rawWaypoints.split(WAYPOINTS_SEPARATOR).filter((value) => value.length > 0)
    : [];
  // What THIS decode actually recovered -- start + finish + every
  // successfully-parsed intermediate waypoint.
  const actualStopCount = 2 + waypoints.length;

  const rawStopCount = params.get(PARAM_KEYS.stopCount);
  const parsedStopCount = rawStopCount !== null ? Number(rawStopCount) : NaN;
  const expectedStopCount = Number.isFinite(parsedStopCount) ? parsedStopCount : undefined;

  // D-11's active guard: a `stopCount` hint greater than what was
  // actually recovered means SOMETHING was lost between encode and this
  // decode (a stale bundle that doesn't recognize `stops`, a truncated
  // URL, etc.) -- warn rather than silently loading a shorter trip.
  const staleClientWarning =
    expectedStopCount !== undefined && expectedStopCount > actualStopCount
      ? `This shared trip expected ${expectedStopCount} stops but only ${actualStopCount} loaded — refresh for the full route.`
      : undefined;

  return {
    start,
    finish,
    startLabel: params.get(PARAM_KEYS.startLabel) ?? start,
    finishLabel: params.get(PARAM_KEYS.finishLabel) ?? finish,
    vehicle: params.get(PARAM_KEYS.vehicle) ?? HERO_VEHICLE_PRESET_ID,
    waypoints: waypoints.length > 0 ? waypoints : undefined,
    stopCount: expectedStopCount ?? (waypoints.length > 0 ? actualStopCount : undefined),
    staleClientWarning,
  };
}

// --- Cross-section "load this trip" bridge --------------------------------
// RecentTripsSection (a Sidebar section sibling of PlannerFormSection) needs
// to hand a clicked trip to PlannerFormSection so it can repopulate the
// form and re-solve, without either module importing the other and without
// growing App.tsx/RoutePlanContext.ts for a single self-contained feature.
// A tiny module-level store subscribed to via useSyncExternalStore -- the
// same technique useRecentTrips.ts uses to keep its own sibling readers in
// sync -- carries exactly one pending request, tagged with a nonce so
// re-clicking the same trip still re-fires PlannerFormSection's effect.
export interface LoadTripRequest {
  trip: TripState;
  nonce: number;
}

let pendingLoadRequest: LoadTripRequest | null = null;
let loadRequestNonce = 0;
const loadTripListeners = new Set<() => void>();

export function requestLoadTrip(trip: TripState): void {
  loadRequestNonce += 1;
  pendingLoadRequest = { trip, nonce: loadRequestNonce };
  loadTripListeners.forEach((listener) => listener());
}

export function subscribeLoadTripRequest(listener: () => void): () => void {
  loadTripListeners.add(listener);
  return () => {
    loadTripListeners.delete(listener);
  };
}

export function getLoadTripRequestSnapshot(): LoadTripRequest | null {
  return pendingLoadRequest;
}
