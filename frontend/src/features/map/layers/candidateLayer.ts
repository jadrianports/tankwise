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
// hue (D-33/09-UI-SPEC.md Data Visualization). Cheapest to most expensive.
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
// candidate_stations[] fields (D-10) -- no transformation beyond
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
// disagree (Don't-Hand-Roll, D-33).
export function buildCircleColorExpression(thresholds: number[]): ExpressionSpecification {
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
    // Deliberately no `cluster`/`clusterRadius` on the source (D-11) -- a
    // cluster bubble averages away the cheap/expensive texture this layer
    // exists to show.
  };
}

/**
 * Adds the candidate source/layer the first time it's needed and updates
 * data + thresholds in place thereafter. Called both on first map load and
 * from inside `useMapStyle`'s `style.load` re-add callback, since a
 * genuine style reload (streets<->satellite) discards every custom
 * source/layer (09-RESEARCH.md Pitfall 1) -- this is what makes the
 * candidate layer survive a satellite switch.
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

  map.setLayoutProperty(CANDIDATE_LAYER_ID, 'visibility', visible ? 'visible' : 'none');

  return thresholds;
}
