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
}

const PARAM_KEYS = {
  start: 'start',
  finish: 'finish',
  startLabel: 'from',
  finishLabel: 'to',
  vehicle: 'vehicle',
} as const;

// Readable query params -- e.g.
// `?start=34.05%2C-118.24&finish=40.71%2C-74.01&from=Los+Angeles&to=New+York&vehicle=semi-loaded`,
// short enough to paste anywhere and legible in a browser address bar.
export function encodeTripState(state: TripState): URLSearchParams {
  const params = new URLSearchParams();
  params.set(PARAM_KEYS.start, state.start);
  params.set(PARAM_KEYS.finish, state.finish);
  if (state.startLabel) params.set(PARAM_KEYS.startLabel, state.startLabel);
  if (state.finishLabel) params.set(PARAM_KEYS.finishLabel, state.finishLabel);
  params.set(PARAM_KEYS.vehicle, state.vehicle);
  return params;
}

export function tripStateToQueryString(state: TripState): string {
  return encodeTripState(state).toString();
}

// Returns null when the minimum required params (start/finish) are
// missing -- a malformed or partial query string is not a valid trip.
export function decodeTripState(search: string | URLSearchParams): TripState | null {
  const params = typeof search === 'string' ? new URLSearchParams(search) : search;
  const start = params.get(PARAM_KEYS.start);
  const finish = params.get(PARAM_KEYS.finish);
  if (!start || !finish) return null;

  return {
    start,
    finish,
    startLabel: params.get(PARAM_KEYS.startLabel) ?? start,
    finishLabel: params.get(PARAM_KEYS.finishLabel) ?? finish,
    vehicle: params.get(PARAM_KEYS.vehicle) ?? HERO_VEHICLE_PRESET_ID,
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
