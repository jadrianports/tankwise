import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';

import StopList from './StopList';
import { RoutePlanContext } from '../../context/RoutePlanContext';
import type { RoutePlanContextValue } from '../../context/RoutePlanContext';
import type { FuelStop } from '../../types/routeContract';

// This file's vite config runs without vitest's `globals` option, so
// testing-library's auto-cleanup detection never fires -- each render
// must be torn down explicitly between tests.
afterEach(cleanup);

const BASE_CONTEXT: RoutePlanContextValue = {
  status: 'idle',
  data: null,
  error: null,
  solve: async () => {},
  retry: () => {},
  focusStop: () => {},
  resolveVehicle: () => {},
};

// Only the fields StopList actually renders or keys off -- one stop with a
// station id, one with a null id to exercise the index fallback.
const STOPS = [
  {
    name: 'Pilot Travel Center',
    station_id: 'ST-9',
    gallons: '58.62',
    price_per_gallon: '3.459',
    cost: '202.79',
  },
  {
    name: 'Loves Travel Stop',
    station_id: null,
    gallons: '40.10',
    price_per_gallon: '3.512',
    cost: '140.86',
  },
] as unknown as FuelStop[];

test('StopList renders nothing when handed an empty stop array', () => {
  const { container } = render(
    <RoutePlanContext.Provider value={{ ...BASE_CONTEXT }}>
      <StopList stops={[]} />
    </RoutePlanContext.Provider>
  );

  expect(container).toBeEmptyDOMElement();
});

test('StopList renders one row per stop showing each stop name', () => {
  render(
    <RoutePlanContext.Provider value={{ ...BASE_CONTEXT }}>
      <StopList stops={STOPS} />
    </RoutePlanContext.Provider>
  );

  expect(screen.getByText('Pilot Travel Center')).toBeInTheDocument();
  expect(screen.getByText('Loves Travel Stop')).toBeInTheDocument();
});

test('clicking a StopList row calls focusStop with that stop station identifier', () => {
  const focusStop = vi.fn();

  render(
    <RoutePlanContext.Provider value={{ ...BASE_CONTEXT, focusStop }}>
      <StopList stops={STOPS} />
    </RoutePlanContext.Provider>
  );

  fireEvent.click(screen.getByText('Pilot Travel Center'));

  expect(focusStop).toHaveBeenCalledWith('ST-9');
});

test('clicking a StopList row with no station id falls back to its index', () => {
  const focusStop = vi.fn();

  render(
    <RoutePlanContext.Provider value={{ ...BASE_CONTEXT, focusStop }}>
      <StopList stops={STOPS} />
    </RoutePlanContext.Provider>
  );

  fireEvent.click(screen.getByText('Loves Travel Stop'));

  expect(focusStop).toHaveBeenCalledWith(1);
});
