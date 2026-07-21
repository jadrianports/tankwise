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

function tripKey(trip: TripState): string {
  return `${trip.start}|${trip.finish}|${trip.vehicle}`;
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
