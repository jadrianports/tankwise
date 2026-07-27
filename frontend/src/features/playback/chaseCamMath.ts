// Pure chase-cam pacing/framing math -- no React, no mapbox-gl import.
// Consumed by useChaseCam.ts, which owns the imperative camera moves;
// this module only decides HOW LONG each beat runs and HOW HIGH the
// camera flies. Kept dependency-light so it is unit-testable without a
// live map instance, mirroring elevationMath.ts.

// Target runtime. A viewer's patience is constant, not proportional to
// route distance -- but this is a target, not a guarantee, because the
// per-beat floors below cannot be compressed indefinitely.
export const TOTAL_BUDGET_MS = 25_000;

// Hard ceiling, enforced by scaleToBudget(). Previously nothing enforced
// one: the budget arithmetic reserved dwell and travel but omitted both
// INITIAL_FLY_MS and the per-stop ease-out beat, and MIN_LEG_MS could
// override the travel budget outright. A 23-stop coast-to-coast trip
// therefore ran ~45.6s against a documented "~20-30s".
export const MAX_TOTAL_MS = 32_000;

export const DWELL_BUDGET_SHARE = 0.5;
export const MIN_DWELL_MS = 900;
export const MAX_DWELL_MS = 2_600;
export const MIN_TRAVEL_BUDGET_MS = 6_000;
export const MIN_LEG_MS = 650;
export const INITIAL_FLY_MS = 800;
export const EASE_OUT_MS = 500;

// Absolute floors. Beats scale toward these together when the ceiling
// binds, so the rhythm degrades evenly instead of one beat collapsing.
export const ABS_MIN_DWELL_MS = 550;
export const ABS_MIN_LEG_MS = 400;
export const ABS_MIN_EASE_MS = 260;

// Approximate ground width of a ~1200px viewport at zoom 0, in miles, at
// mid-US latitudes. Only ever used as a ratio, so the exact viewport
// width matters far less than keeping the camera's altitude proportional
// to how far the leg actually travels.
export const VISIBLE_MI_AT_ZOOM_0 = 44_700;

// How many screen-widths of ground a single leg's flight should cover.
// At the old fixed zoom 14 (~3mi visible) a 500-mile leg covered ~180
// screen-widths in 500ms -- roughly 600 miles per second, which reads as
// a blur AND forces a large raster/DEM tile fetch mid-flight, the actual
// source of the stutter.
export const SCREEN_WIDTHS_PER_LEG = 3;

export const MIN_TRAVEL_ZOOM = 7.5;
export const MAX_TRAVEL_ZOOM = 14;

export interface PlaybackTimeline {
  dwellMs: number;
  easeMs: number;
  travelMs: number[];
  projectedTotalMs: number;
}

function clamp(value: number, lo: number, hi: number): number {
  if (!Number.isFinite(value)) return lo;
  return Math.min(hi, Math.max(lo, value));
}

// Camera altitude for a leg: short legs stay low and cinematic, long
// legs pull up so the flight reads as motion rather than a smear.
export function travelZoomForLeg(legMi: number): number {
  const distance = Number.isFinite(legMi) ? Math.max(legMi, 0.5) : 0.5;
  const targetVisibleMi = distance / SCREEN_WIDTHS_PER_LEG;
  const zoom = Math.log2(VISIBLE_MI_AT_ZOOM_0 / targetVisibleMi);
  return clamp(zoom, MIN_TRAVEL_ZOOM, MAX_TRAVEL_ZOOM);
}

// Every beat the timeline will actually spend, including the ones the
// previous budget arithmetic ignored.
export function projectedTotalMs(
  stopCount: number,
  dwellMs: number,
  easeMs: number,
  travelMs: number[]
): number {
  const travel = travelMs.reduce((sum, ms) => sum + ms, 0);
  return INITIAL_FLY_MS + (dwellMs + easeMs) * stopCount + travel;
}

export function buildPlaybackTimeline(
  stopCount: number,
  legDistancesMi: number[]
): PlaybackTimeline {
  const legCount = legDistancesMi.length;
  const safeStops = Math.max(0, stopCount);

  let dwellMs =
    safeStops > 0
      ? clamp((TOTAL_BUDGET_MS * DWELL_BUDGET_SHARE) / safeStops, MIN_DWELL_MS, MAX_DWELL_MS)
      : 0;
  let easeMs = safeStops > 0 ? EASE_OUT_MS : 0;

  const reserved = INITIAL_FLY_MS + (dwellMs + easeMs) * safeStops;
  const travelBudget = Math.max(TOTAL_BUDGET_MS - reserved, MIN_TRAVEL_BUDGET_MS);
  const totalDistance = legDistancesMi.reduce((sum, d) => sum + d, 0) || 1;
  let travelMs = legDistancesMi.map((d) =>
    Math.max(MIN_LEG_MS, (d / totalDistance) * travelBudget)
  );

  // Enforce the ceiling by scaling every compressible beat toward its own
  // absolute floor by one shared factor.
  let projected = projectedTotalMs(safeStops, dwellMs, easeMs, travelMs);
  if (projected > MAX_TOTAL_MS) {
    const floor =
      INITIAL_FLY_MS +
      (ABS_MIN_DWELL_MS + ABS_MIN_EASE_MS) * safeStops +
      ABS_MIN_LEG_MS * legCount;
    const room = projected - floor;
    const k = room > 0 ? clamp((MAX_TOTAL_MS - floor) / room, 0, 1) : 0;
    dwellMs = ABS_MIN_DWELL_MS + (dwellMs - ABS_MIN_DWELL_MS) * k;
    easeMs = ABS_MIN_EASE_MS + (easeMs - ABS_MIN_EASE_MS) * k;
    travelMs = travelMs.map((ms) => ABS_MIN_LEG_MS + (ms - ABS_MIN_LEG_MS) * k);
    if (safeStops === 0) {
      dwellMs = 0;
      easeMs = 0;
    }
    projected = projectedTotalMs(safeStops, dwellMs, easeMs, travelMs);
  }

  return { dwellMs, easeMs, travelMs, projectedTotalMs: projected };
}
