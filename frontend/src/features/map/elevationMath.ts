// Pure elevation-profile math (ELEV-01/03) -- no React, no mapbox-gl import.
// Consumed by useElevationProfile.ts, which supplies the raw
// queryTerrainElevation() readings; this module only does sampling
// geometry (turf) and series/stats derivation. Kept dependency-light so
// it is trivially unit-testable without a live map instance.

import type { Feature, LineString } from 'geojson';
import along from '@turf/along';
import length from '@turf/length';

import type { ElevationProfile, ElevationSample, ElevationStats } from '../../types/elevationProfile';

// ~200 evenly-distance-spaced points is a smoothness/perf starting point
// (D-08) -- route geometry vertex density varies wildly (dense in cities,
// sparse on rural interstate), so sampling by distance keeps the x-axis
// uniform.
export const SAMPLE_COUNT = 200;

// queryTerrainElevation returns meters ASL; the whole app displays
// imperial units (miles, mpg, gallons), so the conversion happens once,
// immediately after null-interpolation -- never carry a mixed-unit array.
export const METERS_TO_FEET = 3.28084;

// Starting-point threshold (Pitfall 6/D-06): below this total-ascent-in-feet,
// the route is classified "mostly flat" so the chart doesn't let
// @mui/x-charts' auto-scaled y-axis dramatize a few dozen feet of noise.
export const MOSTLY_FLAT_THRESHOLD_FT = 250;

function toLineStringFeature(routeGeometry: [number, number][]): Feature<LineString> {
  return {
    type: 'Feature',
    properties: {},
    geometry: { type: 'LineString', coordinates: routeGeometry },
  };
}

// Even-distance sampling (D-08): SAMPLE_COUNT+1 points from 0mi to the
// route's own turf-computed total length, NEVER the API's total_route_mi
// (a separately-computed backend Decimal -- mixing the two would drift the
// hover-marker's turf.along() lookups near the route ends, see RESEARCH.md
// Pitfall 7).
export function buildEvenSamples(routeGeometry: [number, number][]): ElevationSample[] {
  if (routeGeometry.length < 2) return [];

  const line = toLineStringFeature(routeGeometry);
  const totalMi = length(line, { units: 'miles' });
  const samples: ElevationSample[] = [];

  for (let i = 0; i <= SAMPLE_COUNT; i += 1) {
    const distanceMi = (totalMi * i) / SAMPLE_COUNT;
    const point = along(line, distanceMi, { units: 'miles' });
    const [lng, lat] = point.geometry.coordinates;
    if (!Number.isFinite(lng) || !Number.isFinite(lat) || !Number.isFinite(distanceMi)) continue;
    samples.push({ distanceMi, lng, lat });
  }

  return samples;
}

// An isolated null sample (tile-boundary miss, momentary loading gap) is
// interpolated from its neighbors; a leading/trailing run of nulls clamps
// to the nearest known value (no extrapolation past the route's own
// endpoints); an entirely-null input yields all-zeros -- the caller
// (assembleElevationProfile) treats that case as 'unavailable', not a
// fake flat line.
export function interpolateNulls(metersOrNull: (number | null)[]): number[] {
  const result: (number | null)[] = [...metersOrNull];

  for (let i = 0; i < result.length; i += 1) {
    if (result[i] !== null) continue;

    let before = i - 1;
    while (before >= 0 && result[before] === null) before -= 1;

    let after = i + 1;
    while (after < result.length && result[after] === null) after += 1;

    if (before >= 0 && after < result.length) {
      const span = after - before;
      const frac = (i - before) / span;
      result[i] = (result[before] as number) * (1 - frac) + (result[after] as number) * frac;
    } else if (before >= 0) {
      result[i] = result[before];
    } else if (after < result.length) {
      result[i] = result[after];
    } else {
      result[i] = 0;
    }
  }

  return result.map((value) => (Number.isFinite(value) ? (value as number) : 0));
}

export function metersToFeet(m: number): number {
  if (!Number.isFinite(m)) return 0;
  return m * METERS_TO_FEET;
}

// These are stats over a SAMPLED series, not a survey, and their accuracy
// is bounded by the caller's input rather than by anything here:
// `useElevationProfile` reads `queryTerrainElevation`, which only sees DEM
// tiles already decoded at the map's current zoom. Sampling therefore
// happens at the fitBounds overview zoom, where the terrain is smoothed.
// Measured on Denver->Salt Lake City: overview-zoom sampling reported a
// route minimum of ~4,615ft against ~4,231ft from a higher-zoom sample of
// the identical geometry (the finish, downtown SLC, is ~4,226ft), and
// total ascent of ~9,000ft against ~11,800ft. It is reproducible run to
// run, so this is an accuracy limit, not instability -- ElevationChart
// presents the result as approximate for that reason. Raising the
// SAMPLE_COUNT above does NOT help: the limit is DEM resolution, not
// sample density. Closing it properly means sourcing elevation
// independently of the map camera (e.g. decoding Terrain-RGB tiles at a
// fixed zoom), which trades accuracy against the project's
// minimal-external-calls constraint.
export function computeElevationStats(elevationsFt: number[]): ElevationStats {
  if (elevationsFt.length === 0) {
    return { minFt: 0, maxFt: 0, ascentFt: 0, descentFt: 0 };
  }

  let minFt = elevationsFt[0];
  let maxFt = elevationsFt[0];
  let ascentFt = 0;
  let descentFt = 0;

  for (let i = 0; i < elevationsFt.length; i += 1) {
    const value = elevationsFt[i];
    if (value < minFt) minFt = value;
    if (value > maxFt) maxFt = value;
    if (i > 0) {
      const delta = value - elevationsFt[i - 1];
      if (delta > 0) ascentFt += delta;
      else descentFt += -delta;
    }
  }

  return {
    minFt: Number.isFinite(minFt) ? minFt : 0,
    maxFt: Number.isFinite(maxFt) ? maxFt : 0,
    ascentFt: Number.isFinite(ascentFt) ? ascentFt : 0,
    descentFt: Number.isFinite(descentFt) ? descentFt : 0,
  };
}

export function isMostlyFlat(stats: ElevationStats): boolean {
  return stats.ascentFt < MOSTLY_FLAT_THRESHOLD_FT;
}

// Composes the pure fns above into the typed ElevationProfile the hook
// returns. Detects the all-null case BEFORE interpolation so a genuinely
// unavailable terrain response never gets classified as a fake flat route.
export function assembleElevationProfile(
  samples: ElevationSample[],
  rawMetersOrNull: (number | null)[],
): ElevationProfile {
  const distances = samples.map((sample) => (Number.isFinite(sample.distanceMi) ? sample.distanceMi : 0));
  const totalMi = distances.length > 0 ? distances[distances.length - 1] : 0;

  const isAllNull = rawMetersOrNull.length === 0 || rawMetersOrNull.every((value) => value === null);
  if (samples.length === 0 || isAllNull) {
    return {
      status: 'unavailable',
      distances,
      elevationsFt: distances.map(() => 0),
      totalMi,
      stats: { minFt: 0, maxFt: 0, ascentFt: 0, descentFt: 0 },
    };
  }

  const elevationsFt = interpolateNulls(rawMetersOrNull).map((meters) => metersToFeet(meters));
  const stats = computeElevationStats(elevationsFt);

  return {
    status: isMostlyFlat(stats) ? 'mostly-flat' : 'ok',
    distances,
    elevationsFt,
    totalMi,
    stats,
  };
}
