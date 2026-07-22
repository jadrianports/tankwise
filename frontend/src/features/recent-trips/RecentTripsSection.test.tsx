import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { expect, test, beforeEach, afterEach, vi } from 'vitest';

import type { RoutePlanContextValue } from '../../context/RoutePlanContext';

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

  render(
    <RoutePlanContext.Provider value={{ ...BASE_CONTEXT }}>
      <RecentTripsSection />
    </RoutePlanContext.Provider>
  );

  expect(screen.getByText('Alpha → Bravo')).toBeInTheDocument();
  expect(screen.getByText('Charlie → Delta')).toBeInTheDocument();
  expect(screen.getAllByRole('listitem')).toHaveLength(2);
});

test("clicking a row invokes the trip-load bridge with that row's trip", async () => {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(SEEDED_TRIPS));
  const { default: RecentTripsSection } = await import('./RecentTripsSection');
  const { RoutePlanContext } = await import('../../context/RoutePlanContext');
  const { requestLoadTrip } = await import('../share-export/tripState');

  render(
    <RoutePlanContext.Provider value={{ ...BASE_CONTEXT }}>
      <RecentTripsSection />
    </RoutePlanContext.Provider>
  );

  fireEvent.click(screen.getByText('Alpha → Bravo'));

  expect(requestLoadTrip).toHaveBeenCalledTimes(1);
  expect(requestLoadTrip).toHaveBeenCalledWith(expect.objectContaining({ start: 'a', finish: 'b' }));
});

test("clicking a row's remove control drops that row from the rendered list", async () => {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(SEEDED_TRIPS));
  const { default: RecentTripsSection } = await import('./RecentTripsSection');
  const { RoutePlanContext } = await import('../../context/RoutePlanContext');

  render(
    <RoutePlanContext.Provider value={{ ...BASE_CONTEXT }}>
      <RecentTripsSection />
    </RoutePlanContext.Provider>
  );

  fireEvent.click(screen.getByRole('button', { name: /Remove Alpha to Bravo from recent trips/i }));

  expect(screen.queryByText('Alpha → Bravo')).not.toBeInTheDocument();
  expect(screen.getByText('Charlie → Delta')).toBeInTheDocument();
});

test('while the context status indicates a solve is in flight, the row buttons render disabled', async () => {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(SEEDED_TRIPS));
  const { default: RecentTripsSection } = await import('./RecentTripsSection');
  const { RoutePlanContext } = await import('../../context/RoutePlanContext');

  render(
    <RoutePlanContext.Provider value={{ ...BASE_CONTEXT, status: 'loading' }}>
      <RecentTripsSection />
    </RoutePlanContext.Provider>
  );

  expect(screen.getByText('Alpha → Bravo').closest('[role="button"]')).toHaveAttribute('aria-disabled', 'true');
});
