import { render, screen, cleanup } from '@testing-library/react';
import { afterEach, expect, test } from 'vitest';

import LegBreakdown from './LegBreakdown';
import type { Leg, WaypointMarker } from '../../types/routeContract';

// This file's vite config runs without vitest's `globals` option, so
// testing-library's auto-cleanup detection never fires -- each render
// must be torn down explicitly between tests.
afterEach(cleanup);

const LEGS = [
  {
    from: 'Start',
    to: 'Pilot Travel Center',
    distance_mi: '210.5',
    duration_s: 12600,
    gallons: '21.05',
    cost: '72.80',
  },
  {
    from: 'Pilot Travel Center',
    to: 'Finish',
    distance_mi: '390.2',
    duration_s: 23400,
    gallons: '39.02',
    cost: '134.96',
  },
] as unknown as Leg[];

test('LegBreakdown renders a summary line combining the formatted duration with a pluralised stop count', () => {
  render(<LegBreakdown legs={LEGS} totalDurationS={36000} fuelStopCount={2} />);

  expect(screen.getByText('10h 0m driving · 2 fuel stops')).toBeInTheDocument();
});

test('LegBreakdown uses the singular form for exactly one fuel stop', () => {
  render(<LegBreakdown legs={LEGS} totalDurationS={36000} fuelStopCount={1} />);

  expect(screen.getByText('10h 0m driving · 1 fuel stop')).toBeInTheDocument();
});

test('LegBreakdown renders one table row per leg', () => {
  render(<LegBreakdown legs={LEGS} totalDurationS={36000} fuelStopCount={2} />);

  // one header row plus one row per leg
  expect(screen.getAllByRole('row')).toHaveLength(LEGS.length + 1);
});

test('LegBreakdown renders the summary line and an empty table body when handed zero legs', () => {
  render(<LegBreakdown legs={[]} totalDurationS={0} fuelStopCount={0} />);

  expect(screen.getByText('0m driving · 0 fuel stops')).toBeInTheDocument();
  expect(screen.getAllByRole('row')).toHaveLength(1);
});

// A/B/C waypoint markers (WAY-06/WAY-08): A = start (distance 0), C =
// finish (distance = total route miles), B is the one intermediate stop,
// positioned inside leg 0's own 0-210.5mi range.
const THREE_STOP_WAYPOINTS = [
  { label: 'A', name: 'START', lat: 34.0522, lng: -118.2437, distance_from_start_mi: '0', duration_s: 0 },
  { label: 'B', name: 'Stop B', lat: 39.7392, lng: -104.9903, distance_from_start_mi: '150.0', duration_s: 9000 },
  { label: 'C', name: 'FINISH', lat: 41.8781, lng: -87.6298, distance_from_start_mi: '600.7', duration_s: 36000 },
] as unknown as WaypointMarker[];

test('LegBreakdown interleaves an intermediate waypoint as a distinct boundary row with its letter and cumulative driving time', () => {
  render(<LegBreakdown legs={LEGS} totalDurationS={36000} fuelStopCount={2} waypoints={THREE_STOP_WAYPOINTS} />);

  expect(screen.getByText(/B · ~2h 30m from start/)).toBeInTheDocument();
  // header + 2 leg rows + 1 boundary row for B (never one for A or C --
  // those are already named by the first/last leg's own from/to text).
  expect(screen.getAllByRole('row')).toHaveLength(LEGS.length + 1 + 1);
});

test('LegBreakdown renders zero boundary rows for a 2-point response (no intermediate waypoints)', () => {
  const twoPointWaypoints = [THREE_STOP_WAYPOINTS[0], THREE_STOP_WAYPOINTS[2]];
  render(<LegBreakdown legs={LEGS} totalDurationS={36000} fuelStopCount={2} waypoints={twoPointWaypoints} />);

  expect(screen.queryByText(/from start/)).not.toBeInTheDocument();
  expect(screen.getAllByRole('row')).toHaveLength(LEGS.length + 1);
});

test('LegBreakdown renders zero boundary rows when no waypoints prop is passed at all', () => {
  render(<LegBreakdown legs={LEGS} totalDurationS={36000} fuelStopCount={2} />);

  expect(screen.queryByText(/from start/)).not.toBeInTheDocument();
  expect(screen.getAllByRole('row')).toHaveLength(LEGS.length + 1);
});
