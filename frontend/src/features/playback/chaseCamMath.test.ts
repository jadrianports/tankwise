import { describe, expect, it } from 'vitest';

import {
  ABS_MIN_LEG_MS,
  MAX_TOTAL_MS,
  MAX_TRAVEL_ZOOM,
  MIN_TRAVEL_ZOOM,
  buildPlaybackTimeline,
  projectedTotalMs,
  travelZoomForLeg,
} from './chaseCamMath';

// Real leg shapes measured against the live API, so these pin the exact
// trips that exposed the original pacing bugs rather than synthetic ones.
const CROSS_COUNTRY_LEGS = Array.from({ length: 24 }, () => 3578 / 24); // 23 stops
const LA_NYC_LEGS = Array.from({ length: 16 }, () => 2781 / 16); // 15 stops
const SHORT_LEGS = [120, 260, 180]; // 2 stops

describe('travelZoomForLeg', () => {
  it('keeps short legs low and cinematic', () => {
    expect(travelZoomForLeg(2)).toBe(MAX_TRAVEL_ZOOM);
    expect(travelZoomForLeg(8)).toBeGreaterThan(13);
  });

  it('pulls the camera up as the leg gets longer', () => {
    const near = travelZoomForLeg(20);
    const mid = travelZoomForLeg(150);
    const far = travelZoomForLeg(500);
    expect(near).toBeGreaterThan(mid);
    expect(mid).toBeGreaterThan(far);
  });

  it('never leaves the usable zoom band, even for absurd input', () => {
    for (const mi of [0, -5, 0.001, 5000, Number.NaN, Number.POSITIVE_INFINITY]) {
      const z = travelZoomForLeg(mi);
      expect(z).toBeGreaterThanOrEqual(MIN_TRAVEL_ZOOM);
      expect(z).toBeLessThanOrEqual(MAX_TRAVEL_ZOOM);
    }
  });

  it('frames a long leg within a few screen-widths rather than hundreds', () => {
    // Regression: at the old fixed zoom 14 a ~150mi leg spanned ~55
    // screen-widths and was flown in 500ms -- the blur/stutter report.
    const legMi = 150;
    const visibleMi = 44_700 / 2 ** travelZoomForLeg(legMi);
    expect(legMi / visibleMi).toBeLessThan(5);
  });
});

describe('buildPlaybackTimeline', () => {
  it('never exceeds the hard ceiling on a many-stop coast-to-coast trip', () => {
    // Regression: this trip ran ~45.6s because the per-stop ease-out beat
    // and the initial fly were absent from the budget arithmetic.
    const t = buildPlaybackTimeline(23, CROSS_COUNTRY_LEGS);
    expect(t.projectedTotalMs).toBeLessThanOrEqual(MAX_TOTAL_MS);
  });

  it.each([
    ['cross-country (23 stops)', 23, CROSS_COUNTRY_LEGS],
    ['LA-NYC (15 stops)', 15, LA_NYC_LEGS],
    ['short trip (2 stops)', 2, SHORT_LEGS],
    ['no stops', 0, [400]],
  ])('respects the ceiling for %s', (_label, stops, legs) => {
    const t = buildPlaybackTimeline(stops as number, legs as number[]);
    expect(t.projectedTotalMs).toBeLessThanOrEqual(MAX_TOTAL_MS);
  });

  it('reports a total that actually accounts for every beat', () => {
    const stops = 15;
    const t = buildPlaybackTimeline(stops, LA_NYC_LEGS);
    expect(t.projectedTotalMs).toBeCloseTo(
      projectedTotalMs(stops, t.dwellMs, t.easeMs, t.travelMs),
      6
    );
  });

  it('keeps every leg above the absolute floor', () => {
    const t = buildPlaybackTimeline(23, CROSS_COUNTRY_LEGS);
    for (const ms of t.travelMs) {
      expect(ms).toBeGreaterThanOrEqual(ABS_MIN_LEG_MS - 1e-6);
    }
  });

  it('gives a longer leg more time than a shorter one', () => {
    const t = buildPlaybackTimeline(2, [50, 400, 100]);
    expect(t.travelMs[1]).toBeGreaterThan(t.travelMs[0]);
    expect(t.travelMs[1]).toBeGreaterThan(t.travelMs[2]);
  });

  it('produces one travel beat per leg', () => {
    expect(buildPlaybackTimeline(2, SHORT_LEGS).travelMs).toHaveLength(SHORT_LEGS.length);
  });

  it('spends no dwell or ease time when there are no stops', () => {
    const t = buildPlaybackTimeline(0, [400]);
    expect(t.dwellMs).toBe(0);
    expect(t.easeMs).toBe(0);
  });

  it('degrades gracefully on zero-distance legs', () => {
    const t = buildPlaybackTimeline(1, [0, 0]);
    expect(Number.isFinite(t.projectedTotalMs)).toBe(true);
    expect(t.projectedTotalMs).toBeLessThanOrEqual(MAX_TOTAL_MS);
  });
});
