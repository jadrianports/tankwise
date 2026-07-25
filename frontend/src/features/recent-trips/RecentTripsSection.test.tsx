import { render, fireEvent, cleanup, within } from '@testing-library/react';
import { expect, test, beforeEach, afterEach, vi } from 'vitest';

import { RoutePlanContext } from '../../context/RoutePlanContext';
import type { RoutePlanContextValue } from '../../context/RoutePlanContext';
import { requestLoadTrip } from '../share-export/tripState';

import RecentTripsSection from './RecentTripsSection';
import { resetRecentTripsStoreForTests } from './useRecentTrips';

// This spec used to give every test its own module instance -- a
// `vi.resetModules()` in `beforeEach` plus a fresh dynamic `import()` of
// the component in each test -- because useRecentTrips' store is seeded
// from localStorage once, at import time, and seeding storage after the
// import would otherwise be invisible.
//
// That pattern was the cause of a long-running intermittent failure: each
// test mounted a tree from a *different* module instance, so a slow
// `await import()` under CPU contention could interleave with the previous
// test's unmount and leave its markup in `document.body`. The next test's
// `screen` query then matched two nodes and threw testing-library's "found
// multiple elements" error. It reproduced only in loaded full-suite runs
// and never in 25 consecutive isolated runs of this file, which is exactly
// the signature of a timing-dependent ordering bug rather than leaked
// state -- per-file localStorage isolation was separately verified intact.
//
// Static imports plus an explicit store reset remove the race by
// construction: one module instance, one store, one mounted tree at a
// time. Queries stay scoped to `container` as a second line of defence.
vi.mock('../share-export/tripState', async () => {
  const actual = await vi.importActual<typeof import('../share-export/tripState')>('../share-export/tripState');
  return {
    ...actual,
    requestLoadTrip: vi.fn(),
  };
});

const STORAGE_KEY = 'spotter.recentTrips.v1';

const BASE_CONTEXT: RoutePlanContextValue = {
  status: 'idle',
  data: null,
  error: null,
  solve: async () => {},
  retry: () => {},
  focusStop: () => {},
  resolveVehicle: () => {},
  elevationProfile: null,
  setHoveredElevationDistanceMi: () => {},
};

const SEEDED_TRIPS = [
  { start: 'a', finish: 'b', startLabel: 'Alpha', finishLabel: 'Bravo', vehicle: 'semi-loaded', savedAt: 1 },
  { start: 'c', finish: 'd', startLabel: 'Charlie', finishLabel: 'Delta', vehicle: 'semi-loaded', savedAt: 2 },
];

// Seed storage and re-read it into the store, so the next render sees the
// seeded trips without needing a fresh module instance.
function seedTrips(): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(SEEDED_TRIPS));
  resetRecentTripsStoreForTests();
}

function renderSection(context: Partial<RoutePlanContextValue> = {}) {
  return render(
    <RoutePlanContext.Provider value={{ ...BASE_CONTEXT, ...context }}>
      <RecentTripsSection />
    </RoutePlanContext.Provider>
  );
}

beforeEach(() => {
  localStorage.clear();
  resetRecentTripsStoreForTests();
  vi.mocked(requestLoadTrip).mockClear();
});

afterEach(() => {
  cleanup();
});

test('with an empty trip store, the component renders nothing at all', () => {
  const { container } = renderSection();

  expect(container).toBeEmptyDOMElement();
});

test('with a seeded trip store, one row is rendered per stored trip showing its start and finish labels', () => {
  seedTrips();

  const scoped = within(renderSection().container);

  expect(scoped.getByText('Alpha → Bravo')).toBeInTheDocument();
  expect(scoped.getByText('Charlie → Delta')).toBeInTheDocument();
  expect(scoped.getAllByRole('listitem')).toHaveLength(2);
});

test("clicking a row invokes the trip-load bridge with that row's trip", () => {
  seedTrips();

  const scoped = within(renderSection().container);
  fireEvent.click(scoped.getByText('Alpha → Bravo'));

  expect(requestLoadTrip).toHaveBeenCalledTimes(1);
  expect(requestLoadTrip).toHaveBeenCalledWith(expect.objectContaining({ start: 'a', finish: 'b' }));
});

test("clicking a row's remove control drops that row from the rendered list", () => {
  seedTrips();

  const scoped = within(renderSection().container);
  fireEvent.click(scoped.getByRole('button', { name: /Remove Alpha to Bravo from recent trips/i }));

  expect(scoped.queryByText('Alpha → Bravo')).not.toBeInTheDocument();
  expect(scoped.getByText('Charlie → Delta')).toBeInTheDocument();
});

test('while the context status indicates a solve is in flight, the row buttons render disabled', () => {
  seedTrips();

  const scoped = within(renderSection({ status: 'loading' }).container);

  expect(scoped.getByText('Alpha → Bravo').closest('[role="button"]')).toHaveAttribute('aria-disabled', 'true');
});
