// Client-only types for the elevation-vs-distance profile (ELEV-01/03).
// The series is computed entirely in the browser from already-loaded
// Mapbox terrain (queryTerrainElevation) -- it is NEVER part of the
// /api/route wire contract, so these types stay out of routeContract.ts.

export type ElevationProfileStatus = 'ok' | 'mostly-flat' | 'unavailable';

export interface ElevationStats {
  minFt: number;
  maxFt: number;
  ascentFt: number;
  descentFt: number;
}

// One evenly-distance-spaced sample point along the route (D-08).
export interface ElevationSample {
  distanceMi: number;
  lng: number;
  lat: number;
}

export interface ElevationProfile {
  status: ElevationProfileStatus;
  distances: number[];
  elevationsFt: number[];
  totalMi: number;
  stats: ElevationStats;
}
