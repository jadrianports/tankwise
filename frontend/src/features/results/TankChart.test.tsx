import { render, screen, cleanup } from '@testing-library/react';
import { afterAll, afterEach, beforeAll, expect, test } from 'vitest';

import TankChart from './TankChart';
import type { FuelStop, Leg, VehicleEcho, WaypointMarker } from '../../types/routeContract';

// This file's vite config runs without vitest's `globals` option, so
// testing-library's auto-cleanup detection never fires -- each render
// must be torn down explicitly between tests.
afterEach(cleanup);

// @mui/x-charts sizes itself from `getComputedStyle(container).width`
// (see useChartDimensions.ts), which jsdom never resolves from CSS layout
// -- only literal, explicitly-set values. Without a real width the chart's
// drawing area collapses to ~0px and a ChartsReferenceLine silently
// declines to render (its own `xPosition < left || xPosition > left +
// width` guard). This mock supplies a fixed usable size for every element
// queried in this file only -- scoped to this test file via
// beforeAll/afterAll, restored immediately after, and touches no other
// suite's jsdom environment.
let originalGetComputedStyle: typeof window.getComputedStyle;

beforeAll(() => {
  originalGetComputedStyle = window.getComputedStyle;
  window.getComputedStyle = ((element: Element, pseudoElt?: string | null) => {
    const style = originalGetComputedStyle(element, pseudoElt ?? undefined);
    return new Proxy(style, {
      get(target, prop, receiver) {
        if (prop === 'width') return '600px';
        if (prop === 'height') return '220px';
        return Reflect.get(target, prop, receiver);
      },
    });
  }) as typeof window.getComputedStyle;
});

afterAll(() => {
  window.getComputedStyle = originalGetComputedStyle;
});

const LEGS = [
  { from: 'Start', to: 'Pilot Travel Center', distance_mi: '210.5', duration_s: 12600, gallons: '21.05', cost: '72.80' },
  { from: 'Pilot Travel Center', to: 'Finish', distance_mi: '390.2', duration_s: 23400, gallons: '39.02', cost: '134.96' },
] as unknown as Leg[];

const STOPS = [
  { name: 'Pilot Travel Center', station_id: 'ST-9', distance_from_start_mi: '210.5', gallons: '21.05', price_per_gallon: '3.45', cost: '72.80' },
] as unknown as FuelStop[];

const VEHICLE = { mpg: '10', tank_range_mi: '500', starting_fuel: '1', starting_fuel_mi: '500' } as unknown as VehicleEcho;

const THREE_STOP_WAYPOINTS = [
  { label: 'A', name: 'START', lat: 34.0522, lng: -118.2437, distance_from_start_mi: '0', duration_s: 0 },
  { label: 'B', name: 'Stop B', lat: 39.7392, lng: -104.9903, distance_from_start_mi: '150.0', duration_s: 9000 },
  { label: 'C', name: 'FINISH', lat: 41.8781, lng: -87.6298, distance_from_start_mi: '600.7', duration_s: 36000 },
] as unknown as WaypointMarker[];

test('TankChart renders a vertical reference marker labeled with the intermediate waypoint letter', () => {
  render(<TankChart legs={LEGS} stops={STOPS} vehicle={VEHICLE} waypoints={THREE_STOP_WAYPOINTS} />);

  expect(screen.getByText('B')).toBeInTheDocument();
});

test('TankChart renders no reference markers for a 2-point response (no intermediate waypoints)', () => {
  const twoPointWaypoints = [THREE_STOP_WAYPOINTS[0], THREE_STOP_WAYPOINTS[2]];
  render(<TankChart legs={LEGS} stops={STOPS} vehicle={VEHICLE} waypoints={twoPointWaypoints} />);

  expect(screen.queryByText('A')).not.toBeInTheDocument();
  expect(screen.queryByText('C')).not.toBeInTheDocument();
});

test('TankChart renders no reference markers when no waypoints prop is passed at all', () => {
  render(<TankChart legs={LEGS} stops={STOPS} vehicle={VEHICLE} />);

  expect(screen.queryByText('B')).not.toBeInTheDocument();
});

test('TankChart still shows the not-enough-data message when the vehicle is missing, unaffected by waypoints', () => {
  render(<TankChart legs={[]} stops={[]} vehicle={null} waypoints={THREE_STOP_WAYPOINTS} />);

  expect(screen.getByText('Not enough trip data to draw a tank chart.')).toBeInTheDocument();
});
