import { render, screen, cleanup } from '@testing-library/react';
import { afterEach, expect, test } from 'vitest';

import LegBreakdown from './LegBreakdown';
import type { Leg } from '../../types/routeContract';

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
