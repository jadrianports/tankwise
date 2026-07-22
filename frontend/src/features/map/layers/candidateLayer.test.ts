import { expect, test, vi } from 'vitest';

import {
  applyCandidateLayer,
  buildCandidateGeoJSON,
  buildCircleColorExpression,
  candidatePrices,
  CANDIDATE_LAYER_ID,
} from './candidateLayer';

const CANDIDATES = [
  { station_id: 'ST-1', lat: 39.1, lng: -104.9, price_per_gallon: '3.10', distance_from_start_mi: '12.5' },
  { station_id: 'ST-2', lat: 39.2, lng: -104.8, price_per_gallon: '3.50', distance_from_start_mi: '52.5' },
];

function fakeMap() {
  return {
    getSource: vi.fn(),
    addSource: vi.fn(),
    getLayer: vi.fn(),
    addLayer: vi.fn(),
    setPaintProperty: vi.fn(),
    setLayoutProperty: vi.fn(),
  };
}

test('candidatePrices extracts the numeric price list and returns an empty array for empty input', () => {
  expect(candidatePrices(CANDIDATES)).toEqual([3.1, 3.5]);
  expect(candidatePrices([])).toEqual([]);
});

test('buildCandidateGeoJSON returns a FeatureCollection whose feature count matches the input candidate count', () => {
  const geojson = buildCandidateGeoJSON(CANDIDATES);
  expect(geojson.type).toBe('FeatureCollection');
  expect(geojson.features.length).toBe(CANDIDATES.length);
});

test('buildCircleColorExpression returns a flat constant colour string when thresholds is empty', () => {
  expect(buildCircleColorExpression([])).toBe('#FFFFD4');
});

test('buildCircleColorExpression returns a step-style expression when thresholds are present', () => {
  const expression = buildCircleColorExpression([3.2, 3.5]);
  expect(Array.isArray(expression)).toBe(true);
  expect((expression as unknown[])[0]).toBe('step');
});

test('applyCandidateLayer adds a source and a layer on first call when neither exists yet', () => {
  const map = fakeMap();
  applyCandidateLayer(map as never, CANDIDATES as never, true);
  expect(map.addSource).toHaveBeenCalledOnce();
  expect(map.addLayer).toHaveBeenCalledOnce();
});

test('applyCandidateLayer skips re-adding when the source and layer already exist', () => {
  const map = fakeMap();
  const fakeSource = { setData: vi.fn() };
  map.getSource.mockReturnValue(fakeSource);
  map.getLayer.mockReturnValue({});
  applyCandidateLayer(map as never, CANDIDATES as never, true);
  expect(map.addSource).not.toHaveBeenCalled();
  expect(map.addLayer).not.toHaveBeenCalled();
  expect(fakeSource.setData).toHaveBeenCalledOnce();
  expect(map.setPaintProperty).toHaveBeenCalledOnce();
});

test('applyCandidateLayer sets the layout visibility property according to the visible argument', () => {
  // setLayoutProperty is only reached once the style already reports the
  // layer present (see the source's own defensive re-check comment), so the
  // double reports an already-added layer for this assertion.
  const hiddenMap = fakeMap();
  hiddenMap.getLayer.mockReturnValue({});
  applyCandidateLayer(hiddenMap as never, CANDIDATES as never, false);
  expect(hiddenMap.setLayoutProperty).toHaveBeenCalledWith(CANDIDATE_LAYER_ID, 'visibility', 'none');

  const visibleMap = fakeMap();
  visibleMap.getLayer.mockReturnValue({});
  applyCandidateLayer(visibleMap as never, CANDIDATES as never, true);
  expect(visibleMap.setLayoutProperty).toHaveBeenCalledWith(CANDIDATE_LAYER_ID, 'visibility', 'visible');
});
