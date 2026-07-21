import { expect, test } from 'vitest';
import type { Feature, LineString, Point } from 'geojson';

import { buildTripGeoJson } from './geoJsonExport';
import type { RouteResponse } from '../../types/routeContract';

// Only `route_geometry`/`fuel_stops` are exercised by buildTripGeoJson -- the
// fixture is cast through `unknown` rather than filling in every unrelated
// RouteResponse field this function never reads.
const FIXTURE = {
  route_geometry: [
    [-118.2437, 34.0522],
    [-74.006, 40.7128],
  ],
  fuel_stops: [
    {
      name: 'Pilot Travel Center',
      station_id: 'ST-1',
      location: { latitude: '36.1', longitude: '-112.3' },
      distance_from_start_mi: '210.5',
      price_per_gallon: '3.459',
      gallons: '58.62',
      cost: '202.79',
    },
  ],
  candidate_stations: [
    { station_id: 'CAND-1', lat: 35.0, lng: -111.0, price_per_gallon: '3.2', distance_from_start_mi: '100' },
  ],
} as unknown as RouteResponse;

test('buildTripGeoJson emits the route as the first feature, a LineString in route_geometry order', () => {
  const geojson = buildTripGeoJson(FIXTURE);
  expect(geojson.type).toBe('FeatureCollection');
  const routeFeature = geojson.features[0] as Feature<LineString>;
  expect(routeFeature.geometry.type).toBe('LineString');
  expect(routeFeature.geometry.coordinates).toEqual(FIXTURE.route_geometry);
});

test('buildTripGeoJson emits one Point per chosen stop with plan facts as properties', () => {
  const geojson = buildTripGeoJson(FIXTURE);
  const stopFeature = geojson.features[1] as Feature<Point>;
  expect(stopFeature.geometry.type).toBe('Point');
  expect(stopFeature.geometry.coordinates).toEqual([-112.3, 36.1]);
  expect(stopFeature.properties?.stop_number).toBe(1);
  expect(stopFeature.properties?.name).toBe('Pilot Travel Center');
  expect(stopFeature.properties?.cost).toBe('202.79');
});

test('buildTripGeoJson never reads candidate_stations -- candidates are map texture, not trip data', () => {
  const geojson = buildTripGeoJson(FIXTURE);
  const serialized = JSON.stringify(geojson);
  expect(serialized.includes('CAND-1')).toBe(false);
  expect(geojson.features.length).toBe(2); // route + 1 chosen stop, no candidates
});

test('buildTripGeoJson skips a stop with no resolvable location rather than emitting a NaN point', () => {
  const geojson = buildTripGeoJson({
    ...FIXTURE,
    fuel_stops: [{ ...FIXTURE.fuel_stops[0], location: null }],
  });
  expect(geojson.features.length).toBe(1); // route only
});
