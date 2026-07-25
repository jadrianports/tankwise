import { renderHook, act } from '@testing-library/react';
import { expect, test, beforeEach } from 'vitest';

import { useRecentTrips, resetRecentTripsStoreForTests } from './useRecentTrips';

// The trip list is a module-scoped binding seeded from localStorage at
// import time, and vitest reloads the module graph between test *files*,
// not between blocks in one file. This spec used to work around that with
// `vi.resetModules()` plus a fresh `await import(...)` per block, which
// handed each block its own module instance.
//
// That pattern caused a real intermittent failure in the sibling
// RecentTripsSection spec: an `await` mid-test can exceed the test timeout
// under CPU load, at which point vitest fails the test and moves on while
// the async continuation keeps running -- so a later render landed in
// `document.body` after cleanup had swept, and the next block matched two
// trees. Re-seeding the store in place lets every block below be fully
// synchronous, which removes that window by construction.
const STORAGE_KEY = 'spotter.recentTrips.v1';
const MAX_ENTRIES = 5;

// Re-read localStorage into the module store, so a block that seeds
// storage sees it on the next render without a fresh module instance.
function reseedStoreFromStorage(): void {
  resetRecentTripsStoreForTests();
}

beforeEach(() => {
  localStorage.clear();
  reseedStoreFromStorage();
});

test('add prepends a trip so the newest entry is first in trips', () => {
  const { result } = renderHook(() => useRecentTrips());

  act(() => {
    result.current.add({ start: 'a', finish: 'b', startLabel: 'A', finishLabel: 'B', vehicle: 'semi-loaded' });
  });
  act(() => {
    result.current.add({ start: 'c', finish: 'd', startLabel: 'C', finishLabel: 'D', vehicle: 'semi-loaded' });
  });

  expect(result.current.trips).toHaveLength(2);
  expect(result.current.trips[0].start).toBe('c');
  expect(result.current.trips[1].start).toBe('a');
});

test('add dedupes by identity fields, leaving length unchanged and moving the match to the front', () => {
  const { result } = renderHook(() => useRecentTrips());

  act(() => {
    result.current.add({ start: 'a', finish: 'b', startLabel: 'A', finishLabel: 'B', vehicle: 'semi-loaded' });
  });
  act(() => {
    result.current.add({ start: 'c', finish: 'd', startLabel: 'C', finishLabel: 'D', vehicle: 'semi-loaded' });
  });
  act(() => {
    result.current.add({ start: 'a', finish: 'b', startLabel: 'A again', finishLabel: 'B again', vehicle: 'semi-loaded' });
  });

  expect(result.current.trips).toHaveLength(2);
  expect(result.current.trips[0].startLabel).toBe('A again');
  expect(result.current.trips[1].start).toBe('c');
});

test('add keeps two multi-stop trips sharing start/finish/vehicle but differing waypoints as distinct entries', () => {
  const { result } = renderHook(() => useRecentTrips());

  act(() => {
    result.current.add({
      start: '34.0522,-118.2437',
      finish: '41.8781,-87.6298',
      startLabel: 'LA',
      finishLabel: 'Chicago',
      vehicle: 'semi-loaded',
      waypoints: ['39.7392,-104.9903'],
    });
  });
  act(() => {
    result.current.add({
      start: '34.0522,-118.2437',
      finish: '41.8781,-87.6298',
      startLabel: 'LA',
      finishLabel: 'Chicago',
      vehicle: 'semi-loaded',
      waypoints: ['33.4484,-112.0740'],
    });
  });

  expect(result.current.trips).toHaveLength(2);
});

test('add still dedupes two multi-stop trips sharing identical waypoints, in the same order', () => {
  const { result } = renderHook(() => useRecentTrips());
  const trip = {
    start: '34.0522,-118.2437',
    finish: '41.8781,-87.6298',
    startLabel: 'LA',
    finishLabel: 'Chicago',
    vehicle: 'semi-loaded',
    waypoints: ['39.7392,-104.9903'],
  };

  act(() => {
    result.current.add(trip);
  });
  act(() => {
    result.current.add(trip);
  });

  expect(result.current.trips).toHaveLength(1);
});

test('add truncates the list to the module max-entries cap', () => {
  const { result } = renderHook(() => useRecentTrips());

  for (let i = 0; i < MAX_ENTRIES + 2; i += 1) {
    act(() => {
      result.current.add({
        start: `start-${i}`,
        finish: `finish-${i}`,
        startLabel: `Start ${i}`,
        finishLabel: `Finish ${i}`,
        vehicle: 'semi-loaded',
      });
    });
  }

  expect(result.current.trips).toHaveLength(MAX_ENTRIES);
  // The oldest two entries (index 0 and 1) should have been pushed out --
  // the newest entry (the last one added) survives at the front.
  expect(result.current.trips[0].start).toBe(`start-${MAX_ENTRIES + 1}`);
  expect(result.current.trips.some((trip) => trip.start === 'start-0')).toBe(false);
  expect(result.current.trips.some((trip) => trip.start === 'start-1')).toBe(false);
});

test('add persists to localStorage under the module storage key, readable as JSON', () => {
  const { result } = renderHook(() => useRecentTrips());

  act(() => {
    result.current.add({ start: 'a', finish: 'b', startLabel: 'A', finishLabel: 'B', vehicle: 'semi-loaded' });
  });

  const raw = localStorage.getItem(STORAGE_KEY);
  expect(raw).not.toBeNull();
  const parsed = JSON.parse(raw as string);
  expect(Array.isArray(parsed)).toBe(true);
  expect(parsed).toHaveLength(1);
  expect(parsed[0].start).toBe('a');
});

test('remove deletes the entry at the given index and leaves the rest in order', () => {
  const { result } = renderHook(() => useRecentTrips());

  act(() => {
    result.current.add({ start: 'a', finish: 'b', startLabel: 'A', finishLabel: 'B', vehicle: 'semi-loaded' });
  });
  act(() => {
    result.current.add({ start: 'c', finish: 'd', startLabel: 'C', finishLabel: 'D', vehicle: 'semi-loaded' });
  });
  act(() => {
    result.current.add({ start: 'e', finish: 'f', startLabel: 'E', finishLabel: 'F', vehicle: 'semi-loaded' });
  });
  // trips are now, newest first: e, c, a. Remove index 1 (c).
  act(() => {
    result.current.remove(1);
  });

  expect(result.current.trips).toHaveLength(2);
  expect(result.current.trips[0].start).toBe('e');
  expect(result.current.trips[1].start).toBe('a');
});

test('a hook mounted with a pre-seeded localStorage entry exposes those trips on first render', () => {
  const seeded = [{ start: 'x', finish: 'y', startLabel: 'X', finishLabel: 'Y', vehicle: 'semi-loaded', savedAt: 1 }];
  localStorage.setItem(STORAGE_KEY, JSON.stringify(seeded));
  reseedStoreFromStorage();

  const { result } = renderHook(() => useRecentTrips());

  expect(result.current.trips).toHaveLength(1);
  expect(result.current.trips[0].start).toBe('x');
});

test('a malformed localStorage value yields an empty trip list rather than throwing', () => {
  localStorage.setItem(STORAGE_KEY, 'not valid json{{{');
  reseedStoreFromStorage();

  expect(() => renderHook(() => useRecentTrips())).not.toThrow();

  const { result } = renderHook(() => useRecentTrips());
  expect(result.current.trips).toEqual([]);
});

test('an absent localStorage value also yields an empty trip list', () => {
  const { result } = renderHook(() => useRecentTrips());

  expect(result.current.trips).toEqual([]);
});
