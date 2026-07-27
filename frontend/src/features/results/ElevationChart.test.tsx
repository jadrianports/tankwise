import { render, screen, cleanup } from '@testing-library/react';
import { afterAll, afterEach, beforeAll, expect, test, vi } from 'vitest';

import ElevationChart from './ElevationChart';
import { LineChart } from '@mui/x-charts/LineChart';
import type { ElevationProfile } from '../../types/elevationProfile';
import type { WaypointMarker } from '../../types/routeContract';

// Wraps the REAL LineChart by default (so the reference-line/chip tests
// below render exactly like TankChart.test.tsx does), but lets the one
// hover-interaction test below swap in a one-time fake implementation that
// invokes the received onHighlightedAxisChange directly with a fabricated
// params array -- simulating a real pointer hover over an @mui/x-charts
// SVG is impractical under jsdom (see PLAN.md's own note on this).
vi.mock('@mui/x-charts/LineChart', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@mui/x-charts/LineChart')>();
  return {
    ...actual,
    LineChart: vi.fn((props: any) => <actual.LineChart {...props} />),
  };
});

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
// suite's jsdom environment. Copied verbatim from TankChart.test.tsx.
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

const OK_PROFILE: ElevationProfile = {
  status: 'ok',
  distances: [0, 50, 100, 150],
  elevationsFt: [300, 950, 550, 800],
  totalMi: 150,
  stats: { minFt: 300, maxFt: 950, ascentFt: 900, descentFt: 400 },
};

const MOSTLY_FLAT_PROFILE: ElevationProfile = {
  status: 'mostly-flat',
  distances: [0, 21, 42.4],
  elevationsFt: [610, 615, 605],
  totalMi: 42.4,
  stats: { minFt: 605, maxFt: 615, ascentFt: 5, descentFt: 10 },
};

const UNAVAILABLE_PROFILE: ElevationProfile = {
  status: 'unavailable',
  distances: [],
  elevationsFt: [],
  totalMi: 0,
  stats: { minFt: 0, maxFt: 0, ascentFt: 0, descentFt: 0 },
};

const THREE_STOP_WAYPOINTS = [
  { label: 'A', name: 'START', lat: 34.0522, lng: -118.2437, distance_from_start_mi: '0', duration_s: 0 },
  { label: 'B', name: 'Stop B', lat: 39.7392, lng: -104.9903, distance_from_start_mi: '75', duration_s: 9000 },
  { label: 'C', name: 'FINISH', lat: 41.8781, lng: -87.6298, distance_from_start_mi: '150', duration_s: 18000 },
] as unknown as WaypointMarker[];

test('renders the loading note and no chart when profile is null', () => {
  render(<ElevationChart profile={null} onHoverDistanceChange={vi.fn()} />);

  expect(screen.getByText('Loading elevation profile…')).toBeInTheDocument();
});

test('renders the unavailable note when profile.status is unavailable', () => {
  render(<ElevationChart profile={UNAVAILABLE_PROFILE} onHoverDistanceChange={vi.fn()} />);

  expect(
    screen.getByText(
      "Elevation profile unavailable for this route — terrain data didn't finish loading. The rest of your fuel plan is unaffected.",
    ),
  ).toBeInTheDocument();
});

test('renders the mostly-flat note, with no chart or chips, when profile.status is mostly-flat', () => {
  render(<ElevationChart profile={MOSTLY_FLAT_PROFILE} onHoverDistanceChange={vi.fn()} />);

  expect(screen.getByText('Mostly flat — negligible elevation change over 42 mi.')).toBeInTheDocument();
  expect(screen.queryByText(/climbed/)).not.toBeInTheDocument();
  expect(screen.queryByText(/descended/)).not.toBeInTheDocument();
});

test('renders a stat chip row with approximate feet values for an ok profile', () => {
  render(<ElevationChart profile={OK_PROFILE} onHoverDistanceChange={vi.fn()} />);

  // Values are marked approximate on purpose: the series is sampled from
  // whatever DEM tiles the map has decoded at its current zoom, so a
  // to-the-foot label overstated the precision actually available.
  expect(screen.getByText('Min ~300 ft')).toBeInTheDocument();
  expect(screen.getByText('Max ~950 ft')).toBeInTheDocument();
  expect(screen.getByText('▲ ~900 ft climbed')).toBeInTheDocument();
  expect(screen.getByText('▼ ~400 ft descended')).toBeInTheDocument();
});

test('rounds stat chips to the nearest 50 ft rather than implying survey precision', () => {
  const profile = {
    ...OK_PROFILE,
    stats: { minFt: 4828.4, maxFt: 8002.1, ascentFt: 5959.7, descentFt: 4984.2 },
  };

  render(<ElevationChart profile={profile} onHoverDistanceChange={vi.fn()} />);

  expect(screen.getByText('Min ~4,850 ft')).toBeInTheDocument();
  expect(screen.getByText('Max ~8,000 ft')).toBeInTheDocument();
  expect(screen.getByText('▲ ~5,950 ft climbed')).toBeInTheDocument();
  expect(screen.getByText('▼ ~5,000 ft descended')).toBeInTheDocument();
});

test('tells the reader the elevation stats are sampled from the map view', () => {
  render(<ElevationChart profile={OK_PROFILE} onHoverDistanceChange={vi.fn()} />);

  expect(screen.getByText(/Sampled from map terrain/)).toBeInTheDocument();
});

test('renders a vertical reference marker labeled with the intermediate waypoint letter', () => {
  render(<ElevationChart profile={OK_PROFILE} waypoints={THREE_STOP_WAYPOINTS} onHoverDistanceChange={vi.fn()} />);

  expect(screen.getByText('B')).toBeInTheDocument();
});

test('renders no reference markers for a 2-point response (no intermediate waypoints)', () => {
  const twoPointWaypoints = [THREE_STOP_WAYPOINTS[0], THREE_STOP_WAYPOINTS[2]];
  render(<ElevationChart profile={OK_PROFILE} waypoints={twoPointWaypoints} onHoverDistanceChange={vi.fn()} />);

  expect(screen.queryByText('A')).not.toBeInTheDocument();
  expect(screen.queryByText('C')).not.toBeInTheDocument();
});

test('renders no reference markers when no waypoints prop is passed at all', () => {
  render(<ElevationChart profile={OK_PROFILE} onHoverDistanceChange={vi.fn()} />);

  expect(screen.queryByText('B')).not.toBeInTheDocument();
});

test('hovering the chart invokes onHoverDistanceChange with distances[dataIndex]; leaving invokes it with null', () => {
  const onHoverDistanceChange = vi.fn();
  const mockedLineChart = vi.mocked(LineChart);

  mockedLineChart.mockImplementationOnce(((props: {
    onHighlightedAxisChange: (items: { axisId: string; dataIndex: number }[]) => void;
  }) => {
    props.onHighlightedAxisChange([{ axisId: 'x', dataIndex: 1 }]);
    props.onHighlightedAxisChange([]);
    return null;
  }) as unknown as typeof LineChart);

  render(<ElevationChart profile={OK_PROFILE} onHoverDistanceChange={onHoverDistanceChange} />);

  expect(onHoverDistanceChange).toHaveBeenNthCalledWith(1, OK_PROFILE.distances[1]);
  expect(onHoverDistanceChange).toHaveBeenNthCalledWith(2, null);
});

test('never renders NaN or Infinity for a degenerate stats object', () => {
  const degenerateProfile: ElevationProfile = {
    ...OK_PROFILE,
    stats: { minFt: NaN, maxFt: Infinity, ascentFt: NaN, descentFt: -Infinity },
  };
  render(<ElevationChart profile={degenerateProfile} onHoverDistanceChange={vi.fn()} />);

  expect(screen.queryByText(/NaN/)).not.toBeInTheDocument();
  expect(screen.queryByText(/Infinity/)).not.toBeInTheDocument();
});
