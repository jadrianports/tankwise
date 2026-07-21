import type { Feature, FeatureCollection, Point } from 'geojson';
import type {
  Map as MapboxMap,
  GeoJSONSource,
  CircleLayerSpecification,
  ExpressionSpecification,
} from 'mapbox-gl';

import type { CandidateStation } from '../../../types/routeContract';
import { computeQuantileBins } from '../../../utils/quantile';

export const CANDIDATE_SOURCE_ID = 'candidate-stations';
export const CANDIDATE_LAYER_ID = 'candidate-stations-circle';

// ColorBrewer YlOrBr 5-class -- verified colorblind-safe, contains no red
// hue. Cheapest to most expensive.
export const CANDIDATE_RAMP = ['#FFFFD4', '#FED98E', '#FE9929', '#D95F0E', '#993404'] as const;

export interface CandidateFeatureProperties {
  station_id: string;
  price_per_gallon: number;
  distance_from_start_mi: string;
  [key: string]: string | number;
}

// Extracts the numeric prices `computeQuantileBins` needs -- `price_per_gallon`
// arrives as a Decimal string per the typed contract (routeContract.ts).
export function candidatePrices(candidates: CandidateStation[]): number[] {
  return candidates.map((c) => Number(c.price_per_gallon)).filter((n) => Number.isFinite(n));
}

// Builds the GeoJSON FeatureCollection straight from the five locked
// candidate_stations[] fields -- no transformation beyond
// Number(price_per_gallon) for the paint expression.
export function buildCandidateGeoJSON(
  candidates: CandidateStation[]
): FeatureCollection<Point, CandidateFeatureProperties> {
  const features: Feature<Point, CandidateFeatureProperties>[] = candidates.map((c) => ({
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [c.lng, c.lat] },
    properties: {
      station_id: c.station_id,
      price_per_gallon: Number(c.price_per_gallon),
      distance_from_start_mi: c.distance_from_start_mi,
    },
  }));
  return { type: 'FeatureCollection', features };
}

// A `step` expression keyed on `price_per_gallon`, using the SAME
// thresholds `PriceLegend.tsx` renders -- both call `computeQuantileBins`
// independently against the same candidates array, so they can never
// disagree.
//
// With zero thresholds (a corridor that returned no candidate prices, e.g.
// the initial load before any solve) a `step` would be degenerate -- Mapbox
// requires at least one stop/output pair and rejects `['step', input, base]`,
// throwing on addLayer. Return the cheapest ramp color as a flat constant in
// that case: there is nothing to bin, and the layer has no features anyway.
export function buildCircleColorExpression(thresholds: number[]): ExpressionSpecification | string {
  if (thresholds.length === 0) return CANDIDATE_RAMP[0];
  const expression: ExpressionSpecification = ['step', ['get', 'price_per_gallon'], CANDIDATE_RAMP[0]];
  thresholds.slice(0, CANDIDATE_RAMP.length - 1).forEach((threshold, i) => {
    expression.push(threshold, CANDIDATE_RAMP[i + 1]);
  });
  return expression;
}

function buildCandidateLayerSpec(thresholds: number[]): CircleLayerSpecification {
  return {
    id: CANDIDATE_LAYER_ID,
    type: 'circle',
    source: CANDIDATE_SOURCE_ID,
    paint: {
      'circle-color': buildCircleColorExpression(thresholds),
      'circle-radius': 5,
      'circle-opacity': 0.75,
    },
    // Deliberately no `cluster`/`clusterRadius` on the source -- a
    // cluster bubble averages away the cheap/expensive texture this layer
    // exists to show.
  };
}

/**
 * Adds the candidate source/layer the first time it's needed and updates
 * data + thresholds in place thereafter. Called both on first map load and
 * from inside `useMapStyle`'s `style.load` re-add callback, since a
 * genuine style reload (streets<->satellite) discards every custom
 * source/layer -- this is what makes the candidate layer survive a
 * satellite switch.
 *
 * Returns the computed thresholds (not otherwise consumed by the caller;
 * exposed for symmetry/testability with PriceLegend's identical call).
 */
export function applyCandidateLayer(
  map: MapboxMap,
  candidates: CandidateStation[],
  visible: boolean
): number[] {
  const thresholds = computeQuantileBins(candidatePrices(candidates), 5);
  const geojson = buildCandidateGeoJSON(candidates);

  const source = map.getSource(CANDIDATE_SOURCE_ID) as GeoJSONSource | undefined;
  if (source) {
    source.setData(geojson);
  } else {
    map.addSource(CANDIDATE_SOURCE_ID, { type: 'geojson', data: geojson });
  }

  if (!map.getLayer(CANDIDATE_LAYER_ID)) {
    map.addLayer(buildCandidateLayerSpec(thresholds));
  } else {
    map.setPaintProperty(CANDIDATE_LAYER_ID, 'circle-color', buildCircleColorExpression(thresholds));
  }

  // Guard against a layer that failed to add (defensive -- with the constant
  // colour fallback above addLayer no longer throws on an empty corridor, but
  // never call setLayoutProperty on a layer the style doesn't have).
  if (map.getLayer(CANDIDATE_LAYER_ID)) {
    map.setLayoutProperty(CANDIDATE_LAYER_ID, 'visibility', visible ? 'visible' : 'none');
  }

  return thresholds;
}
