// Shared quantile-bin threshold math (Don't-Hand-Roll, 09-RESEARCH.md): a
// SINGLE function that sorts a price array and returns the ascending
// breakpoints splitting it into `bins` equal-count groups. Both the
// candidate circle layer's `step` color expression and the price legend
// call this exact function against the same `candidate_stations[]` prices
// -- never duplicate this math, or the map and the legend can silently
// drift onto different thresholds (D-33).
//
// Percentiles are corridor-relative and recomputed per response (never
// cached across trips) -- see 09-RESEARCH.md's anti-pattern warning.

/**
 * Splits a sorted copy of `prices` into `bins` equal-count groups and
 * returns the `bins - 1` ascending breakpoint values between them (e.g.
 * `computeQuantileBins(prices, 5)` returns 4 thresholds for 5 bins).
 *
 * Never throws: an empty or very short `prices` array returns a shorter
 * (possibly empty) array rather than throwing, since a route can legitimately
 * return zero or a handful of in-corridor candidates.
 */
export function computeQuantileBins(prices: number[], bins: number): number[] {
  const sorted = prices.filter((n) => Number.isFinite(n)).sort((a, b) => a - b);

  if (sorted.length === 0 || bins <= 1) return [];

  const thresholds: number[] = [];
  for (let i = 1; i < bins; i += 1) {
    const idx = Math.min(Math.floor((sorted.length * i) / bins), sorted.length - 1);
    thresholds.push(sorted[idx]);
  }
  return thresholds;
}
