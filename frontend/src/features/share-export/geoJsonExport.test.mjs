import { test } from 'node:test';
import assert from 'node:assert/strict';

import { buildTripGeoJson } from './geoJsonExport.ts';

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
};

test('buildTripGeoJson emits the route as the first feature, a LineString in route_geometry order', () => {
  const geojson = buildTripGeoJson(FIXTURE);
  assert.equal(geojson.type, 'FeatureCollection');
  assert.equal(geojson.features[0].geometry.type, 'LineString');
  assert.deepEqual(geojson.features[0].geometry.coordinates, FIXTURE.route_geometry);
});

test('buildTripGeoJson emits one Point per chosen stop with plan facts as properties', () => {
  const geojson = buildTripGeoJson(FIXTURE);
  const stopFeature = geojson.features[1];
  assert.equal(stopFeature.geometry.type, 'Point');
  assert.deepEqual(stopFeature.geometry.coordinates, [-112.3, 36.1]);
  assert.equal(stopFeature.properties.stop_number, 1);
  assert.equal(stopFeature.properties.name, 'Pilot Travel Center');
  assert.equal(stopFeature.properties.cost, '202.79');
});

test('buildTripGeoJson never reads candidate_stations -- candidates are map texture, not trip data (D-29)', () => {
  const geojson = buildTripGeoJson(FIXTURE);
  const serialized = JSON.stringify(geojson);
  assert.equal(serialized.includes('CAND-1'), false);
  assert.equal(geojson.features.length, 2); // route + 1 chosen stop, no candidates
});

test('buildTripGeoJson skips a stop with no resolvable location rather than emitting a NaN point', () => {
  const geojson = buildTripGeoJson({
    ...FIXTURE,
    fuel_stops: [{ ...FIXTURE.fuel_stops[0], location: null }],
  });
  assert.equal(geojson.features.length, 1); // route only
});
