import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react';
import { afterEach, expect, test } from 'vitest';

import ResultsSection from './ResultsSection';
import { RoutePlanContext } from '../../context/RoutePlanContext';
import type { RoutePlanContextValue } from '../../context/RoutePlanContext';
import { mapErrorToMessage } from '../../api/routeClient';
import type { RouteResponse } from '../../types/routeContract';

afterEach(cleanup);

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

test('renders the mapErrorToMessage copy inside role="alert" when a plan fails', () => {
  const message = mapErrorToMessage({ code: 'route_not_found', message: 'No route found.', detail: {} });

  render(
    <RoutePlanContext.Provider
      value={{ ...BASE_CONTEXT, status: 'error', error: { code: 'route_not_found', message } }}
    >
      <ResultsSection />
    </RoutePlanContext.Provider>
  );

  expect(screen.getByRole('alert')).toHaveTextContent('No drivable route between these points.');
});

test('shows a Retry button only for an upstream_error, not other error codes', () => {
  const upstreamMessage = mapErrorToMessage({ code: 'upstream_error', message: '', detail: {} });
  const { rerender } = render(
    <RoutePlanContext.Provider
      value={{ ...BASE_CONTEXT, status: 'error', error: { code: 'upstream_error', message: upstreamMessage } }}
    >
      <ResultsSection />
    </RoutePlanContext.Provider>
  );
  expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();

  const notFoundMessage = mapErrorToMessage({ code: 'route_not_found', message: '', detail: {} });
  rerender(
    <RoutePlanContext.Provider
      value={{ ...BASE_CONTEXT, status: 'error', error: { code: 'route_not_found', message: notFoundMessage } }}
    >
      <ResultsSection />
    </RoutePlanContext.Provider>
  );
  expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
});

// Minimal multi-stop RouteResponse fixture (D-13/Pitfall D): a 3-stop
// A/B/C trip, only the fields ResultsSection's own children (SummaryCard/
// LegBreakdown/TankChart/StopList) actually read.
const MULTI_STOP_DATA = {
  total_cost: '245.67',
  vehicle: {
    starting_fuel_mi: '500',
    mpg: '10',
    tank_range_mi: '500',
    starting_fuel: '1',
  },
  savings: { amount: '32.50', percent: 11.7 },
  savings_note: null,
  alternatives_considered: 1,
  price_as_of: '2025-01-01',
  price_data_note: 'Prices reflect the most recent available data.',
  fuel_stops: [],
  legs: [
    { from: 'Start', to: 'Finish', distance_mi: '600.7', duration_s: 36000, gallons: '60.07', cost: '245.67' },
  ],
  total_duration_s: 36000,
  fuel_stop_count: 0,
  waypoints: [
    { label: 'A', name: 'START', lat: 34.0522, lng: -118.2437, distance_from_start_mi: '0', duration_s: 0 },
    { label: 'B', name: 'Stop B', lat: 39.7392, lng: -104.9903, distance_from_start_mi: '150.0', duration_s: 9000 },
    { label: 'C', name: 'FINISH', lat: 41.8781, lng: -87.6298, distance_from_start_mi: '600.7', duration_s: 36000 },
  ],
} as unknown as RouteResponse;

test('the per-leg breakdown renders its waypoint boundary row in the DOM without expanding the accordion, so the print stylesheet has real content to reveal (D-13 print inheritance)', () => {
  render(
    <RoutePlanContext.Provider value={{ ...BASE_CONTEXT, status: 'success', data: MULTI_STOP_DATA }}>
      <ResultsSection />
    </RoutePlanContext.Provider>
  );

  // MUI's Accordion mounts AccordionDetails by default (see print.css's own
  // comment) -- the boundary row is already in the DOM even though the
  // accordion reads as visually collapsed on screen.
  expect(screen.getByText(/B · ~2h 30m from start/)).toBeInTheDocument();
});

test('savings renders as exactly one trip-total figure, never a per-leg breakdown (D-13)', () => {
  render(
    <RoutePlanContext.Provider value={{ ...BASE_CONTEXT, status: 'success', data: MULTI_STOP_DATA }}>
      <ResultsSection />
    </RoutePlanContext.Provider>
  );

  expect(screen.getAllByText(/^Save \$/)).toHaveLength(1);
  expect(screen.getByText('Save $32.50 (11.7%)')).toBeInTheDocument();
});

test('a control named for the multiple-stops question is present and focusable when a plan is rendered', () => {
  render(
    <RoutePlanContext.Provider value={{ ...BASE_CONTEXT, status: 'success', data: MULTI_STOP_DATA }}>
      <ResultsSection />
    </RoutePlanContext.Provider>
  );

  const trigger = screen.getByRole('button', { name: /why multiple fuel stops/i });
  expect(trigger).toBeInTheDocument();
  trigger.focus();
  expect(trigger).toHaveFocus();
});

test('activating the fuel-stops explainer trigger opens the explainer dialog', () => {
  render(
    <RoutePlanContext.Provider value={{ ...BASE_CONTEXT, status: 'success', data: MULTI_STOP_DATA }}>
      <ResultsSection />
    </RoutePlanContext.Provider>
  );

  fireEvent.click(screen.getByRole('button', { name: /why multiple fuel stops/i }));
  expect(screen.getByRole('dialog', { name: /why multiple fuel stops/i })).toBeInTheDocument();
});

test('closing the explainer dialog removes it while the itinerary stays rendered', async () => {
  render(
    <RoutePlanContext.Provider value={{ ...BASE_CONTEXT, status: 'success', data: MULTI_STOP_DATA }}>
      <ResultsSection />
    </RoutePlanContext.Provider>
  );

  fireEvent.click(screen.getByRole('button', { name: /why multiple fuel stops/i }));
  fireEvent.click(screen.getByRole('button', { name: 'Close' }));

  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  expect(screen.getByText('Fuel stops')).toBeInTheDocument();
});

test('the idle state renders neither the fuel-stops header nor the explainer trigger', () => {
  render(
    <RoutePlanContext.Provider value={BASE_CONTEXT}>
      <ResultsSection />
    </RoutePlanContext.Provider>
  );

  expect(screen.queryByText('Fuel stops')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /why multiple fuel stops/i })).not.toBeInTheDocument();
});

test('the existing SummaryCard, accordions, elevation chart, and stop list still render alongside the new header', () => {
  render(
    <RoutePlanContext.Provider value={{ ...BASE_CONTEXT, status: 'success', data: MULTI_STOP_DATA }}>
      <ResultsSection />
    </RoutePlanContext.Provider>
  );

  expect(screen.getByText('Per-leg breakdown')).toBeInTheDocument();
  expect(screen.getByText('Tank level along the route')).toBeInTheDocument();
  expect(screen.getByText('Elevation profile')).toBeInTheDocument();
  expect(screen.getByText('Fuel stops')).toBeInTheDocument();
});
