// The last 5 trip INPUTS (never the solved plan), deduped,
// newest first, stored via tripState.ts's canonical TripState shape so a
// recent trip can never disagree with what the app would compute for the
// same inputs.
//
// Backed by a module-level store (not component state, not React context)
// read through useSyncExternalStore -- every component calling this hook
// stays in sync automatically. This matters because PlannerFormSection
// (adds on submit/demo-chip) and RecentTripsSection (reads/removes) are
// sibling Sidebar sections: two independent useState instances would let
// one add a trip the other never sees until an unrelated re-render.
import { useCallback, useSyncExternalStore } from 'react';

import type { TripState } from '../share-export/tripState';

const STORAGE_KEY = 'spotter.recentTrips.v1';
const MAX_ENTRIES = 5;

export interface RecentTrip extends TripState {
  savedAt: number;
}

function isRecentTrip(value: unknown): value is RecentTrip {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<RecentTrip>;
  return (
    typeof candidate.start === 'string' &&
    typeof candidate.finish === 'string' &&
    typeof candidate.startLabel === 'string' &&
    typeof candidate.finishLabel === 'string' &&
    typeof candidate.vehicle === 'string' &&
    typeof candidate.savedAt === 'number'
  );
}

function readStorage(): RecentTrip[] {
  if (typeof window === 'undefined') return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter(isRecentTrip) : [];
  } catch {
    return [];
  }
}

function writeStorage(trips: RecentTrip[]): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(trips));
  } catch {
    // localStorage unavailable (private browsing, quota exceeded) --
    // degrade silently; recent trips simply won't persist this session.
  }
}

// Additive (D-10, Pitfall C): folds in an order-preserving encoding of
// `waypoints` so two different multi-stop trips sharing the same
// start/finish/vehicle (e.g. "LA->Denver->Chicago" vs
// "LA->Phoenix->Chicago") no longer collide onto the same recent-trips
// slot -- a plain A->B trip (`waypoints` empty/absent) keys identically
// to before this field existed.
function tripKey(trip: TripState): string {
  const waypointsToken = trip.waypoints && trip.waypoints.length > 0 ? trip.waypoints.join(';') : '';
  return `${trip.start}|${trip.finish}|${trip.vehicle}|${waypointsToken}`;
}

let state: RecentTrip[] = readStorage();
const listeners = new Set<() => void>();

function setState(next: RecentTrip[]): void {
  state = next;
  writeStorage(state);
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function getSnapshot(): RecentTrip[] {
  return state;
}

// Test seam. `state` above is seeded from localStorage once, at module
// import time, so a test that seeds localStorage AFTER importing this
// module would otherwise read a stale snapshot. The workaround used to be
// `vi.resetModules()` plus a fresh dynamic `import()` in every test, which
// gave each test its own module instance -- and therefore its own
// `listeners` set and its own mounted React tree racing the previous
// test's cleanup. That raced only under load, which is why it surfaced as
// an intermittent "found multiple elements" failure in full-suite runs and
// never once in 25 isolated runs of the same spec.
//
// Re-seeding in place removes the need for per-test module instances
// entirely: one static import, one store, deterministic ordering. Not
// wired into any production path -- nothing outside a test imports it.
export function resetRecentTripsStoreForTests(): void {
  state = readStorage();
  listeners.forEach((listener) => listener());
}

export interface UseRecentTripsResult {
  trips: RecentTrip[];
  add: (trip: TripState) => void;
  remove: (index: number) => void;
}

export function useRecentTrips(): UseRecentTripsResult {
  const trips = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  const add = useCallback((trip: TripState) => {
    const key = tripKey(trip);
    const deduped = state.filter((existing) => tripKey(existing) !== key);
    setState([{ ...trip, savedAt: Date.now() }, ...deduped].slice(0, MAX_ENTRIES));
  }, []);

  const remove = useCallback((index: number) => {
    setState(state.filter((_, i) => i !== index));
  }, []);

  return { trips, add, remove };
}
