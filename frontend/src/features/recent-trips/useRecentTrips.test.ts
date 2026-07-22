import { renderHook, act } from '@testing-library/react';
import { expect, test, beforeEach, vi } from 'vitest';

// No static top-level import of the module under test -- its trip list is a
// module-scoped binding initialised from localStorage at import time, and
// vitest only reloads the module graph between test *files*, not between
// blocks in one file. Every block below does a fresh `await import(...)`
// after `vi.resetModules()` so it gets a genuinely new module instance,
// instead of the accumulated state of whichever block ran before it.
const STORAGE_KEY = 'spotter.recentTrips.v1';
const MAX_ENTRIES = 5;

beforeEach(() => {
  localStorage.clear();
  vi.resetModules();
});

test('add prepends a trip so the newest entry is first in trips', async () => {
  const { useRecentTrips } = await import('./useRecentTrips');
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

test('add dedupes by identity fields, leaving length unchanged and moving the match to the front', async () => {
  const { useRecentTrips } = await import('./useRecentTrips');
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

test('add truncates the list to the module max-entries cap', async () => {
  const { useRecentTrips } = await import('./useRecentTrips');
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

test('add persists to localStorage under the module storage key, readable as JSON', async () => {
  const { useRecentTrips } = await import('./useRecentTrips');
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

test('remove deletes the entry at the given index and leaves the rest in order', async () => {
  const { useRecentTrips } = await import('./useRecentTrips');
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

test('a hook mounted with a pre-seeded localStorage entry exposes those trips on first render', async () => {
  const seeded = [{ start: 'x', finish: 'y', startLabel: 'X', finishLabel: 'Y', vehicle: 'semi-loaded', savedAt: 1 }];
  localStorage.setItem(STORAGE_KEY, JSON.stringify(seeded));

  const { useRecentTrips } = await import('./useRecentTrips');
  const { result } = renderHook(() => useRecentTrips());

  expect(result.current.trips).toHaveLength(1);
  expect(result.current.trips[0].start).toBe('x');
});

test('a malformed localStorage value yields an empty trip list rather than throwing', async () => {
  localStorage.setItem(STORAGE_KEY, 'not valid json{{{');

  const { useRecentTrips } = await import('./useRecentTrips');
  expect(() => renderHook(() => useRecentTrips())).not.toThrow();

  const { result } = renderHook(() => useRecentTrips());
  expect(result.current.trips).toEqual([]);
});

test('an absent localStorage value also yields an empty trip list', async () => {
  const { useRecentTrips } = await import('./useRecentTrips');
  const { result } = renderHook(() => useRecentTrips());

  expect(result.current.trips).toEqual([]);
});
