import { forwardRef, useImperativeHandle } from 'react';
import type { ReactNode } from 'react';
import { render, screen, cleanup } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';

// This test file needs a Marker/Map mock capable of real assertions
// (rendered pin letters, captured fitBounds/flyTo calls) -- the global
// src/test/setup.ts stub renders `Marker`/`Map` as `() => null` (adequate
// for other suites that never assert on map internals), so this file's
// own `vi.mock` shadows it. MapView.tsx itself stays excluded from the
// coverage report (vite.config.ts) as a stated engineering judgment about
// coverage *metrics*, not a ban on writing behavioral tests for it.
const { fitBoundsMock, flyToMock } = vi.hoisted(() => ({
  fitBoundsMock: vi.fn(),
  flyToMock: vi.fn(),
}));

vi.mock('react-map-gl/mapbox', () => {
  const MapMock = forwardRef<unknown, { children?: ReactNode }>(function MapMock({ children }, ref) {
    useImperativeHandle(ref, () => ({
      fitBounds: fitBoundsMock,
      flyTo: flyToMock,
      getMap: () => null,
    }));
    return <div data-testid="mapbox-map">{children}</div>;
  });

  function MarkerMock({
    children,
    longitude,
    latitude,
  }: {
    children?: ReactNode;
    longitude: number;
    latitude: number;
  }) {
    return (
      <div data-testid="marker" data-lng={longitude} data-lat={latitude}>
        {children}
      </div>
    );
  }

  return {
    default: MapMock,
    Map: MapMock,
    Marker: MarkerMock,
    Source: () => null,
    Layer: () => null,
    NavigationControl: () => null,
  };
});

import MapView from './MapView';
import type { RouteResponse } from '../../types/routeContract';

afterEach(() => {
  cleanup();
  fitBoundsMock.mockClear();
  flyToMock.mockClear();
});

const TWO_POINT_DATA = {
  start: { latitude: '34.0522', longitude: '-118.2437' },
  finish: { latitude: '41.8781', longitude: '-87.6298' },
  route_geometry: [],
  fuel_stops: [],
  candidate_stations: [],
  waypoints: [
    { label: 'A', name: 'START', lat: 34.0522, lng: -118.2437, distance_from_start_mi: '0', duration_s: 0 },
    { label: 'B', name: 'FINISH', lat: 41.8781, lng: -87.6298, distance_from_start_mi: '600.7', duration_s: 36000 },
  ],
  legs: [],
} as unknown as RouteResponse;

const THREE_STOP_DATA = {
  start: { latitude: '34.0522', longitude: '-118.2437' },
  finish: { latitude: '41.8781', longitude: '-87.6298' },
  route_geometry: [],
  fuel_stops: [],
  candidate_stations: [],
  waypoints: [
    { label: 'A', name: 'START', lat: 34.0522, lng: -118.2437, distance_from_start_mi: '0', duration_s: 0 },
    { label: 'B', name: 'Stop B', lat: 39.7392, lng: -104.9903, distance_from_start_mi: '150.0', duration_s: 9000 },
    { label: 'C', name: 'FINISH', lat: 41.8781, lng: -87.6298, distance_from_start_mi: '600.7', duration_s: 36000 },
  ],
  legs: [],
} as unknown as RouteResponse;

test('a 3-stop response renders three lettered pins A, B, C distinct from fuel-icon markers', () => {
  render(<MapView data={THREE_STOP_DATA} token="pk.test" tokenStatus="ready" />);

  expect(screen.getByText('A')).toBeInTheDocument();
  expect(screen.getByText('B')).toBeInTheDocument();
  expect(screen.getByText('C')).toBeInTheDocument();
});

test('a 3-stop response calls fitBounds with a bbox enclosing all three stops, using the standard padding/duration', () => {
  render(<MapView data={THREE_STOP_DATA} token="pk.test" tokenStatus="ready" />);

  expect(fitBoundsMock).toHaveBeenCalledTimes(1);
  const [bounds, options] = fitBoundsMock.mock.calls[0];
  expect(bounds).toEqual([
    [-118.2437, 34.0522],
    [-87.6298, 41.8781],
  ]);
  expect(options).toEqual({ padding: 64, duration: 800 });
});

test('a 2-point response renders exactly two lettered pins (A, B) with the unchanged 2-point camera', () => {
  render(<MapView data={TWO_POINT_DATA} token="pk.test" tokenStatus="ready" />);

  expect(screen.getByText('A')).toBeInTheDocument();
  expect(screen.getByText('B')).toBeInTheDocument();
  expect(screen.queryByText('C')).not.toBeInTheDocument();

  expect(fitBoundsMock).toHaveBeenCalledTimes(1);
  const [bounds] = fitBoundsMock.mock.calls[0];
  expect(bounds).toEqual([
    [-118.2437, 34.0522],
    [-87.6298, 41.8781],
  ]);
});

test('the camera holds position on a re-solve that lands on the identical stop coordinates', () => {
  const { rerender } = render(<MapView data={THREE_STOP_DATA} token="pk.test" tokenStatus="ready" />);
  expect(fitBoundsMock).toHaveBeenCalledTimes(1);

  // A fresh response object/array reference (e.g. a vehicle-slider
  // re-solve) with the exact same resolved stop coordinates -- the
  // fitBounds effect is keyed off coordinates, not the `data`/`waypoints`
  // reference, so the camera must not move again.
  const sameCoordsRerun = {
    ...THREE_STOP_DATA,
    waypoints: THREE_STOP_DATA.waypoints.map((w) => ({ ...w })),
  } as unknown as RouteResponse;
  rerender(<MapView data={sameCoordsRerun} token="pk.test" tokenStatus="ready" />);

  expect(fitBoundsMock).toHaveBeenCalledTimes(1);
});
