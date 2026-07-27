import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { travelZoomForLeg } from './chaseCamMath';
import { useChaseCam } from './useChaseCam';

// The pure pacing/framing math is covered in chaseCamMath.test.ts. This
// file covers the WIRING: that play() actually threads a per-leg travel
// zoom into the camera instead of a single constant, and still runs to
// completion without stalling on an awaited camera move.

interface FlyCall {
  zoom: number;
  duration: number;
  center: [number, number];
}

function makeFakeMap() {
  const flyCalls: FlyCall[] = [];
  const jumpCalls: FlyCall[] = [];
  const listeners = new Map<string, Set<() => void>>();

  const map = {
    flyTo(target: any) {
      flyCalls.push({ zoom: target.zoom, duration: target.duration, center: target.center });
      // resolve the move on the next timer tick, as mapbox-gl would
      setTimeout(() => {
        const handlers = listeners.get('moveend');
        if (handlers) [...handlers].forEach((h) => h());
      }, 0);
    },
    jumpTo(target: any) {
      jumpCalls.push({ zoom: target.zoom, duration: 0, center: target.center });
    },
    on(event: string, handler: () => void) {
      if (!listeners.has(event)) listeners.set(event, new Set());
      listeners.get(event)!.add(handler);
    },
    off(event: string, handler: () => void) {
      listeners.get(event)?.delete(handler);
    },
    once(event: string, handler: () => void) {
      this.on(event, handler);
    },
    stop() {},
    fitBounds() {},
    isMoving: () => false,
    areTilesLoaded: () => true,
    getZoom: () => 5,
  };

  return { map, flyCalls, jumpCalls };
}

const loc = (lat: string, lng: string) => ({ latitude: lat, longitude: lng });

// START -> stop1 is a long haul, stop1 -> stop2 is a short hop. The two
// legs must NOT be flown at the same altitude.
function makeData() {
  return {
    start: loc('34.0522', '-118.2437'),
    finish: loc('41.8781', '-87.6298'),
    fuel_stops: [
      {
        name: 'A',
        station_id: 1,
        location: loc('36.1699', '-115.1398'),
        distance_from_start_mi: '480',
        price_per_gallon: '4.00',
        gallons: '10.00',
        cost: '40.00',
        rationale: { skipped_count: 0, skipped_avg_price: null },
      },
      {
        name: 'B',
        station_id: 2,
        location: loc('36.2000', '-115.2000'),
        distance_from_start_mi: '495',
        price_per_gallon: '4.10',
        gallons: '5.00',
        cost: '20.50',
        rationale: { skipped_count: 0, skipped_avg_price: null },
      },
    ],
    legs: [
      { distance_mi: '480' },
      { distance_mi: '15' },
      { distance_mi: '1300' },
    ],
    candidate_stations: [],
    vehicle: { mpg: '10', tank_range_mi: '500', starting_fuel_mi: '500' },
  } as any;
}

async function drain(ms = 60_000) {
  // advance in slices so interleaved awaits/timers both progress
  for (let i = 0; i < 120; i += 1) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms / 120);
    });
  }
}

describe('useChaseCam wiring', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal('matchMedia', () => ({ matches: false, addListener() {}, removeListener() {} }));
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('flies each leg at an altitude derived from that leg s distance', async () => {
    const { map, flyCalls } = makeFakeMap();
    const { result } = renderHook(() => useChaseCam(map as any, makeData()));

    await act(async () => {
      result.current.play();
    });
    await drain();

    expect(flyCalls.length).toBeGreaterThanOrEqual(3);

    // The 480mi leg must be flown from higher up than the 15mi hop --
    // the regression this guards is both flown at a fixed zoom 14.
    const longLegZoom = travelZoomForLeg(480);
    const shortHopZoom = travelZoomForLeg(15);
    expect(longLegZoom).toBeLessThan(shortHopZoom);

    const zoomsUsed = flyCalls.map((c) => c.zoom);
    expect(zoomsUsed).toContain(longLegZoom);
    expect(zoomsUsed).toContain(shortHopZoom);
    expect(Math.min(...zoomsUsed)).toBeLessThan(14);
  });

  it('runs to completion without stalling on a camera move', async () => {
    const { map } = makeFakeMap();
    const { result } = renderHook(() => useChaseCam(map as any, makeData()));

    await act(async () => {
      result.current.play();
    });
    await drain();

    expect(result.current.status).toBe('finished');
    expect(result.current.currentBeat).toBeNull();
  });

  it('gives every camera move a non-negative duration', async () => {
    const { map, flyCalls } = makeFakeMap();
    const { result } = renderHook(() => useChaseCam(map as any, makeData()));

    await act(async () => {
      result.current.play();
    });
    await drain();

    for (const call of flyCalls) {
      expect(call.duration).toBeGreaterThan(0);
      expect(Number.isFinite(call.duration)).toBe(true);
      expect(Number.isFinite(call.zoom)).toBe(true);
    }
  });
});
