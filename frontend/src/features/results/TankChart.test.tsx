import { render, screen, cleanup } from '@testing-library/react';
import { afterAll, afterEach, beforeAll, expect, test } from 'vitest';

import TankChart from './TankChart';
import { buildTankSeries } from './tankSeries';
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

// Payload shaped the way the API actually emits it: `build_legs`
// (routing/services/legs.py) attributes each purchase to the leg DEPARTING
// the node where it was made, so leg 0 always carries 0.00 gal no matter how
// far it runs, and leg k carries stop k-1's purchase. The fixtures above
// predate that understanding -- their leg 0 carries 21.05 gal, exactly
// 210.5 mi / 10 mpg -- which is why a green suite shipped a flat tank line.
const REAL_SHAPE_LEGS = [
  { from: 'START', to: 'PWI #525', distance_mi: '1035', duration_s: 59241, gallons: '0.00', cost: '0.00' },
  { from: 'PWI #525', to: 'AKAL TRAVEL CENTER', distance_mi: '425', duration_s: 21596, gallons: '63.02', cost: '301.78' },
  { from: 'AKAL TRAVEL CENTER', to: 'FINISH', distance_mi: '563', duration_s: 31292, gallons: '86.55', cost: '362.87' },
] as unknown as Leg[];

const REAL_SHAPE_STOPS = [
  { name: 'PWI #525', station_id: 'A', distance_from_start_mi: '1035', gallons: '63.02', price_per_gallon: '4.79', cost: '301.78' },
  { name: 'AKAL TRAVEL CENTER', station_id: 'B', distance_from_start_mi: '1460', gallons: '86.55', price_per_gallon: '4.19', cost: '362.87' },
] as unknown as FuelStop[];

const SEMI = { mpg: '6.5', tank_range_mi: '1050', starting_fuel: '1', starting_fuel_mi: '1050' } as unknown as VehicleEcho;

test('buildTankSeries burns fuel across a leg instead of treating leg.gallons as consumption', () => {
  const series = buildTankSeries(REAL_SHAPE_LEGS, REAL_SHAPE_STOPS, SEMI)!;
  expect(series).not.toBeNull();

  // 1,035 mi at 6.5 mpg burns ~159.2 gal out of a 161.54 gal tank.
  expect(series.capacityGal).toBeCloseTo(161.54, 1);
  expect(series.levels[0]).toBeCloseTo(161.54, 1);
  expect(series.levels[1]).toBeCloseTo(2.31, 1);
});

test('buildTankSeries produces a sawtooth, never a flat line, on a multi-stop route', () => {
  const series = buildTankSeries(REAL_SHAPE_LEGS, REAL_SHAPE_STOPS, SEMI)!;
  const distinct = new Set(series.levels.map((l) => Math.round(l)));
  expect(distinct.size).toBeGreaterThan(1);

  // The tank must actually be drawn down: at least one sample well below full.
  expect(Math.min(...series.levels)).toBeLessThan(series.capacityGal * 0.5);
});

test('buildTankSeries rises again at every stop where fuel was purchased', () => {
  const series = buildTankSeries(REAL_SHAPE_LEGS, REAL_SHAPE_STOPS, SEMI)!;
  const rises = series.levels.filter((lvl, i) => i > 0 && lvl > series.levels[i - 1]);
  expect(rises).toHaveLength(REAL_SHAPE_STOPS.length);
});

test('buildTankSeries never reports a physically impossible level', () => {
  const series = buildTankSeries(REAL_SHAPE_LEGS, REAL_SHAPE_STOPS, SEMI)!;
  for (const lvl of series.levels) {
    expect(lvl).toBeGreaterThanOrEqual(0);
    expect(lvl).toBeLessThanOrEqual(series.capacityGal + 0.01);
  }
});
