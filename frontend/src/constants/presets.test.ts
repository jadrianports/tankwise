import { expect, test } from 'vitest';

import { PRESET_ROUTES } from './presets';

test('the LA -> Denver -> Chicago chip carries exactly the D-09 coordinates and one waypoint', () => {
  const multiStopTrip = PRESET_ROUTES.find((trip) => trip.label === 'Los Angeles → Denver → Chicago');
  expect(multiStopTrip).toBeDefined();
  expect(multiStopTrip?.start).toBe('34.0522,-118.2437');
  expect(multiStopTrip?.finish).toBe('41.8781,-87.6298');
  expect(multiStopTrip?.waypoints).toEqual(['39.7392,-104.9903']);
});

test('the three original A-to-B chips are unchanged and carry no waypoints key', () => {
  const originalLabels = ['Los Angeles → New York City', 'Dallas → Seattle', 'Catalina Island → Los Angeles'];
  const originalTrips = PRESET_ROUTES.filter((trip) => originalLabels.includes(trip.label));
  expect(originalTrips).toHaveLength(3);
  originalTrips.forEach((trip) => {
    expect(trip.waypoints).toBeUndefined();
  });
});

test('PRESET_ROUTES gains exactly one new multi-stop entry alongside the three existing chips', () => {
  expect(PRESET_ROUTES).toHaveLength(4);
});

test('every demo chip description stays count-agnostic about the number of fuel stops', () => {
  // Anchored on the word "stop"/"stops" rather than any digit, so mileage
  // figures like "~2,790 mi" or "~1,050 mi" never trip this pattern.
  const stopCountPattern = /\d+\s*(fuel\s+)?stops?/i;
  PRESET_ROUTES.forEach((trip) => {
    expect(trip.description).not.toMatch(stopCountPattern);
  });
});

test('the launch lineup is exactly the four confirmed chips in order, with only the Denver chip carrying waypoints', () => {
  expect(PRESET_ROUTES.map((trip) => trip.label)).toEqual([
    'Los Angeles → New York City',
    'Dallas → Seattle',
    'Catalina Island → Los Angeles',
    'Los Angeles → Denver → Chicago',
  ]);
  const withWaypoints = PRESET_ROUTES.filter((trip) => trip.waypoints !== undefined);
  expect(withWaypoints).toHaveLength(1);
  expect(withWaypoints[0].label).toBe('Los Angeles → Denver → Chicago');
});
