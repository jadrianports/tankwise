import { render, fireEvent, cleanup, within } from '@testing-library/react';
import { expect, test, beforeEach, afterEach, vi } from 'vitest';

import type { RoutePlanContextValue } from '../../context/RoutePlanContext';

// KNOWN INTERMITTENT (not resolved): this spec has failed intermittently
// during full-suite gate runs -- twice with a bare `npm test` (no
// verbatim text captured either time until 2026-07-25), and once more
// via frontend/scripts/flake-hunt.mjs's own `--load` campaign condition
// (though that run's failureMessages were a generic vitest runner
// STACK_TRACE_ERROR placeholder produced by severe CPU starvation, not
// the assertion text below -- a different symptom, not necessarily the
// same cause). The one CONFIRMED capture (2026-07-25, a plain `npm test`
// immediately after a burst of file-read activity) was
// `screen.getByText('Alpha → Bravo')` throwing testing-library's
// "multiple elements found" error -- i.e. leftover DOM from an earlier
// render was still present in `document.body` when this test's own
// `screen` query ran, even though both a file-local and the global
// (src/test/setup.ts) `afterEach(cleanup)` are wired up. That prior
// global-cleanup attempt (commit 3490287) did not resolve it, and ~17
// further clean local runs since then never reproduced it.
//
// If this fires again, capture it properly instead of letting the text
// scroll away: `node scripts/flake-hunt.mjs --runs 10` (or
// `--after-build` / `--load <core count>` to match whatever condition
// was running when it happened) from `frontend/`. A failing run's
// verbatim `failureMessages` are printed to stdout immediately and
// retained under `frontend/.flake-hunt/<timestamp>/`.
//
// Below, every query that used to go through the global `screen` object
// (which searches all of `document.body`) is scoped to `within(container)`
// instead -- immunity to a CLASS of cause (any leaked DOM, from anywhere,
// makes this test's assertions safe by construction), not a root-cause
// fix. The actual leak source is still unknown.

// RecentTripsSection reads through useRecentTrips' module-scoped trip list,
// initialised from localStorage at import time -- the same singleton
// Task 1 resets per block. vi.resetModules() plus a fresh dynamic import
// gives each block its own instance not just of the component but of
// everything it transitively imports (the context module and the
// trip-load bridge included), so every value a block reads back --
// the Provider's Context object, the mocked bridge -- is re-imported
// fresh in the same block rather than captured once at file load time.
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

beforeEach(() => {
  localStorage.clear();
  vi.resetModules();
});

// This suite renders a fresh module instance per test (see the vi.resetModules
// note above) without a global `afterEach(cleanup)` wired into setup.ts, so
// each rendered tree must be explicitly unmounted or a later test's queries
// can match leftover DOM from an earlier one.
afterEach(() => {
  cleanup();
});

test('with an empty trip store, the component renders nothing at all', async () => {
  const { default: RecentTripsSection } = await import('./RecentTripsSection');
  const { RoutePlanContext } = await import('../../context/RoutePlanContext');

  const { container } = render(
    <RoutePlanContext.Provider value={{ ...BASE_CONTEXT }}>
      <RecentTripsSection />
    </RoutePlanContext.Provider>
  );

  expect(container).toBeEmptyDOMElement();
});

test('with a seeded trip store, one row is rendered per stored trip showing its start and finish labels', async () => {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(SEEDED_TRIPS));
  const { default: RecentTripsSection } = await import('./RecentTripsSection');
  const { RoutePlanContext } = await import('../../context/RoutePlanContext');

  const { container } = render(
    <RoutePlanContext.Provider value={{ ...BASE_CONTEXT }}>
      <RecentTripsSection />
    </RoutePlanContext.Provider>
  );
  const scoped = within(container);

  expect(scoped.getByText('Alpha → Bravo')).toBeInTheDocument();
  expect(scoped.getByText('Charlie → Delta')).toBeInTheDocument();
  expect(scoped.getAllByRole('listitem')).toHaveLength(2);
});

test("clicking a row invokes the trip-load bridge with that row's trip", async () => {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(SEEDED_TRIPS));
  const { default: RecentTripsSection } = await import('./RecentTripsSection');
  const { RoutePlanContext } = await import('../../context/RoutePlanContext');
  const { requestLoadTrip } = await import('../share-export/tripState');

  const { container } = render(
    <RoutePlanContext.Provider value={{ ...BASE_CONTEXT }}>
      <RecentTripsSection />
    </RoutePlanContext.Provider>
  );
  const scoped = within(container);

  fireEvent.click(scoped.getByText('Alpha → Bravo'));

  expect(requestLoadTrip).toHaveBeenCalledTimes(1);
  expect(requestLoadTrip).toHaveBeenCalledWith(expect.objectContaining({ start: 'a', finish: 'b' }));
});

test("clicking a row's remove control drops that row from the rendered list", async () => {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(SEEDED_TRIPS));
  const { default: RecentTripsSection } = await import('./RecentTripsSection');
  const { RoutePlanContext } = await import('../../context/RoutePlanContext');

  const { container } = render(
    <RoutePlanContext.Provider value={{ ...BASE_CONTEXT }}>
      <RecentTripsSection />
    </RoutePlanContext.Provider>
  );
  const scoped = within(container);

  fireEvent.click(scoped.getByRole('button', { name: /Remove Alpha to Bravo from recent trips/i }));

  expect(scoped.queryByText('Alpha → Bravo')).not.toBeInTheDocument();
  expect(scoped.getByText('Charlie → Delta')).toBeInTheDocument();
});

test('while the context status indicates a solve is in flight, the row buttons render disabled', async () => {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(SEEDED_TRIPS));
  const { default: RecentTripsSection } = await import('./RecentTripsSection');
  const { RoutePlanContext } = await import('../../context/RoutePlanContext');

  const { container } = render(
    <RoutePlanContext.Provider value={{ ...BASE_CONTEXT, status: 'loading' }}>
      <RecentTripsSection />
    </RoutePlanContext.Provider>
  );
  const scoped = within(container);

  expect(scoped.getByText('Alpha → Bravo').closest('[role="button"]')).toHaveAttribute('aria-disabled', 'true');
});
