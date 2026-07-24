import length from '@turf/length';
import { expect, test } from 'vitest';

import {
  MOSTLY_FLAT_THRESHOLD_FT,
  SAMPLE_COUNT,
  assembleElevationProfile,
  buildEvenSamples,
  computeElevationStats,
  interpolateNulls,
  isMostlyFlat,
  metersToFeet,
} from './elevationMath';

const FLAT_ROUTE: [number, number][] = [
  [-100, 40],
  [-99, 40],
];

test('interpolateNulls linearly interpolates a single interior null from its neighbors', () => {
  expect(interpolateNulls([100, null, 300])).toEqual([100, 200, 300]);
});

test('interpolateNulls clamps a leading null-run to the nearest known value', () => {
  expect(interpolateNulls([null, null, 500])).toEqual([500, 500, 500]);
});

test('interpolateNulls clamps a trailing null-run to the nearest known value', () => {
  expect(interpolateNulls([100, null, null])).toEqual([100, 100, 100]);
});

test('interpolateNulls returns all-zeros for an entirely-null input', () => {
  expect(interpolateNulls([null, null, null])).toEqual([0, 0, 0]);
});

test('metersToFeet converts using the 3.28084 factor', () => {
  expect(metersToFeet(1000)).toBeCloseTo(3280.84, 2);
});

test('computeElevationStats reports min/max and summed ascent/descent deltas', () => {
  const stats = computeElevationStats([100, 150, 120, 180]);
  expect(stats.minFt).toBe(100);
  expect(stats.maxFt).toBe(180);
  expect(stats.ascentFt).toBeCloseTo(110, 5); // 100->150 (+50), 120->180 (+60)
  expect(stats.descentFt).toBeCloseTo(30, 5); // 150->120 (-30)
});

test('isMostlyFlat is true below the ~250ft threshold', () => {
  expect(MOSTLY_FLAT_THRESHOLD_FT).toBe(250);
  expect(isMostlyFlat({ minFt: 0, maxFt: 10, ascentFt: 100, descentFt: 90 })).toBe(true);
});

test('isMostlyFlat is false above the threshold', () => {
  expect(isMostlyFlat({ minFt: 0, maxFt: 1000, ascentFt: 300, descentFt: 290 })).toBe(false);
});

test('buildEvenSamples returns SAMPLE_COUNT+1 samples from 0 to the route length', () => {
  const samples = buildEvenSamples(FLAT_ROUTE);
  expect(samples).toHaveLength(SAMPLE_COUNT + 1);
  expect(samples[0].distanceMi).toBe(0);

  const line = { type: 'Feature' as const, properties: {}, geometry: { type: 'LineString' as const, coordinates: FLAT_ROUTE } };
  const totalMi = length(line, { units: 'miles' });
  expect(samples[samples.length - 1].distanceMi).toBeCloseTo(totalMi, 6);

  samples.forEach((sample) => {
    expect(Number.isFinite(sample.lng)).toBe(true);
    expect(Number.isFinite(sample.lat)).toBe(true);
    expect(Number.isFinite(sample.distanceMi)).toBe(true);
  });
});

test('buildEvenSamples returns an empty array for a degenerate (<2 point) route', () => {
  expect(buildEvenSamples([])).toEqual([]);
  expect(buildEvenSamples([[-100, 40]])).toEqual([]);
});

test('assembleElevationProfile flags unavailable for all-null raw terrain input', () => {
  const samples = buildEvenSamples(FLAT_ROUTE);
  const profile = assembleElevationProfile(
    samples,
    samples.map(() => null),
  );
  expect(profile.status).toBe('unavailable');
  profile.elevationsFt.forEach((value) => expect(Number.isFinite(value)).toBe(true));
  expect(Number.isFinite(profile.stats.minFt)).toBe(true);
  expect(Number.isFinite(profile.stats.maxFt)).toBe(true);
});

test('assembleElevationProfile flags mostly-flat for a genuinely flat interpolated series', () => {
  const samples = buildEvenSamples(FLAT_ROUTE);
  const rawMeters = samples.map(() => 300); // constant elevation, zero relief
  const profile = assembleElevationProfile(samples, rawMeters);
  expect(profile.status).toBe('mostly-flat');
});

test('assembleElevationProfile flags ok for a series with real relief', () => {
  const samples = buildEvenSamples(FLAT_ROUTE);
  const rawMeters = samples.map((_, index) => (index % 2 === 0 ? 100 : 500)); // large alternating relief
  const profile = assembleElevationProfile(samples, rawMeters);
  expect(profile.status).toBe('ok');
  expect(Number.isFinite(profile.stats.ascentFt)).toBe(true);
  expect(Number.isFinite(profile.stats.descentFt)).toBe(true);
});
