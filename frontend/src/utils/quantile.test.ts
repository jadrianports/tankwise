import { expect, test } from 'vitest';

import { computeQuantileBins } from './quantile';

test('computeQuantileBins returns bins-1 ascending thresholds for a normal unsorted price array', () => {
  const thresholds = computeQuantileBins([3.1, 3.5, 3.2, 3.9, 3.0], 5);
  expect(thresholds).toEqual([3.1, 3.2, 3.5, 3.9]);
  expect(thresholds.length).toBe(4);
  expect([...thresholds].sort((a, b) => a - b)).toEqual(thresholds);
});

test('computeQuantileBins returns an empty array for empty input', () => {
  expect(computeQuantileBins([], 5)).toEqual([]);
});

test('computeQuantileBins returns an empty array when bins is 1 or lower', () => {
  expect(computeQuantileBins([1, 2, 3], 1)).toEqual([]);
  expect(computeQuantileBins([1, 2, 3], 0)).toEqual([]);
});

test('computeQuantileBins filters non-finite entries before sorting', () => {
  const thresholds = computeQuantileBins([1, NaN, 2, Infinity, 3], 2);
  expect(thresholds).toEqual([2]);
  expect(thresholds.every((n) => Number.isFinite(n))).toBe(true);
});
