import { useCallback, useMemo, useRef, useState } from 'react';
import type { Map as MapboxMap } from 'mapbox-gl';

import type { CandidateStation, FuelStop, RouteResponse } from '../../types/routeContract';

// Fixed ~20-30s total regardless of trip length -- a viewer's
// patience is constant, not proportional to route distance. Per-stop
// dwell is DERIVED from (budget / stop_count), never a hardcoded
// constant: the demo hauls (LA->NYC, Dallas->Seattle) land ~6 stops at
// the shipped vehicle presets, not the 2-3 originally assumed, so a
// fixed per-stop beat sized for 2-3 stops would overrun a 6-stop trip
// badly.
const TOTAL_BUDGET_MS = 25_000;
const DWELL_BUDGET_SHARE = 0.5;
const MIN_DWELL_MS = 900;
const MAX_DWELL_MS = 2_600;
const MIN_TRAVEL_BUDGET_MS = 6_000;
const MIN_LEG_MS = 500;
const INITIAL_FLY_MS = 800;
const EASE_OUT_MS = 500;

// Chase cam is low and pitched and stays pitched for the ENTIRE
// playback regardless of zoom (see the `isPlayback` override MapView.tsx
// passes to useTerrain.ts's `getConditionalPitch`) -- only altitude
// (zoom) changes between the travel and ease-out beats, so neither beat
// ever fights the map's own controlled `pitch` prop, which is otherwise
// derived purely from zoom.
const TRAVEL_ZOOM = 14;
const TRAVEL_PITCH = 55;
const STOP_ZOOM = 10;
const STOP_PITCH = 55;
const FINISH_ZOOM = 12;

export interface ChaseCamBeat {
  index: number;
  stop: FuelStop;
  fuelRemainingMi: number;
  gallonsToppedUp: string;
  pricePaid: string;
  skippedCount: number;
  skippedAvgPrice: string | null;
  skippedCandidates: CandidateStation[];
}

export type ChaseCamStatus = 'idle' | 'playing' | 'finished';

export interface UseChaseCamResult {
  status: ChaseCamStatus;
  currentBeat: ChaseCamBeat | null;
  tankFraction: number;
  canPlay: boolean;
  play: () => void;
  skip: () => void;
  dismiss: () => void;
}

interface CameraTarget {
  center: [number, number];
  zoom: number;
  pitch: number;
  bearing: number;
}

function toRadians(deg: number): number {
  return (deg * Math.PI) / 180;
}
function toDegrees(rad: number): number {
  return (rad * 180) / Math.PI;
}

// Standard initial-bearing great-circle formula -- keeps the chase cam
// facing the direction of travel instead of a fixed compass heading.
function computeBearing(from: [number, number], to: [number, number]): number {
  const [lng1, lat1] = from;
  const [lng2, lat2] = to;
  const phi1 = toRadians(lat1);
  const phi2 = toRadians(lat2);
  const deltaLambda = toRadians(lng2 - lng1);
  const y = Math.sin(deltaLambda) * Math.cos(phi2);
  const x = Math.cos(phi1) * Math.sin(phi2) - Math.sin(phi1) * Math.cos(phi2) * Math.cos(deltaLambda);
  return (toDegrees(Math.atan2(y, x)) + 360) % 360;
}

function clamp01(n: number): number {
  if (!Number.isFinite(n)) return 0;
  return Math.min(1, Math.max(0, n));
}

function wait(ms: number): Promise<void> {
  if (ms <= 0) return Promise.resolve();
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Per-stop dwell is derived from the fixed total budget, not a hardcoded
// constant -- see the TOTAL_BUDGET_MS comment above.
function computeDwellMs(stopCount: number): number {
  if (stopCount <= 0) return 0;
  const raw = (TOTAL_BUDGET_MS * DWELL_BUDGET_SHARE) / stopCount;
  return Math.min(MAX_DWELL_MS, Math.max(MIN_DWELL_MS, raw));
}

// Local, unexported prefers-reduced-motion check. BottomSheet.tsx
// already carries an equivalent independent implementation for its own
// snap-point transition; this hook duplicates the same small check
// rather than extracting a shared one out of a file it otherwise never
// touches.
function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

// Resolves once the map's imperative camera move settles. Under reduced
// motion, `jumpTo` is used directly (an instant jump cut) rather
// than relying on mapbox-gl-js's own built-in prefers-reduced-motion
// handling -- mapbox-gl-js silently skips ANY flyTo/easeTo animation once
// the OS signal is on unless the call is marked `essential: true`, and
// this hook needs to make that branching decision itself (jump cut vs.
// flight), not have mapbox make it invisibly on its own.
function moveCamera(
  map: MapboxMap,
  target: CameraTarget,
  durationMs: number,
  reduced: boolean
): Promise<void> {
  if (reduced || durationMs <= 0) {
    map.jumpTo(target);
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    const handleMoveEnd = () => {
      map.off('moveend', handleMoveEnd);
      resolve();
    };
    map.on('moveend', handleMoveEnd);
    map.flyTo({ ...target, duration: durationMs, essential: true });
  });
}

function locationTuple(
  loc: { latitude: string | null; longitude: string | null } | null | undefined
): [number, number] | null {
  if (!loc?.longitude || !loc?.latitude) return null;
  const lng = Number(loc.longitude);
  const lat = Number(loc.latitude);
  if (!Number.isFinite(lng) || !Number.isFinite(lat)) return null;
  return [lng, lat];
}

// Scripts a fixed-duration chase-cam fly-through of the already-fetched
// response -- composed entirely from `data`'s own legs/fuel_stops/
// candidate_stations/vehicle fields, no new fetch.
export function useChaseCam(map: MapboxMap | null, data: RouteResponse | null): UseChaseCamResult {
  const [status, setStatus] = useState<ChaseCamStatus>('idle');
  const [currentBeat, setCurrentBeat] = useState<ChaseCamBeat | null>(null);
  const [tankFraction, setTankFraction] = useState(1);

  // Monotonic generation guard -- the same sequence-guard pattern
  // useRoutePlan.ts uses to invalidate a stale network response, applied
  // here to a stale in-flight playback instead: `skip()` bumps the
  // generation so every pending `await` inside a still-running `play()`
  // becomes a no-op the moment it resumes, without a separate boolean
  // flag threaded through every step.
  const generationRef = useRef(0);

  const canPlay = Boolean(map && data && data.legs.length > 0);

  const resetCamera = useCallback(() => {
    if (!map || !data) return;
    const start = locationTuple(data.start);
    const finish = locationTuple(data.finish);
    if (!start || !finish) return;
    const reduced = prefersReducedMotion();
    map.fitBounds(
      [
        [Math.min(start[0], finish[0]), Math.min(start[1], finish[1])],
        [Math.max(start[0], finish[0]), Math.max(start[1], finish[1])],
      ],
      { padding: 64, duration: reduced ? 0 : 800 }
    );
  }, [map, data]);

  // Always available; ends playback at any time and returns to the
  // static view -- this is deliberately NOT the same as a natural
  // completion, which ends in the savings finale instead (see `play`'s
  // final `setStatus('finished')`).
  const skip = useCallback(() => {
    if (status === 'idle') return;
    generationRef.current += 1;
    map?.stop();
    setStatus('idle');
    setCurrentBeat(null);
    resetCamera();
  }, [status, map, resetCamera]);

  // Closes the savings finale (a natural completion) and returns to the
  // static view.
  const dismiss = useCallback(() => {
    generationRef.current += 1;
    setStatus('idle');
    setCurrentBeat(null);
    resetCamera();
  }, [resetCamera]);

  const play = useCallback(async () => {
    if (!map || !data || status !== 'idle' || data.legs.length === 0) return;

    const myGeneration = ++generationRef.current;
    const isCurrent = () => myGeneration === generationRef.current;
    const reduced = prefersReducedMotion();

    const stops = data.fuel_stops;
    const legs = data.legs;
    const candidates = data.candidate_stations;
    const vehicle = data.vehicle;

    const mpg = Number(vehicle?.mpg) || 0;
    const tankRangeMi = Number(vehicle?.tank_range_mi) || 1;
    let fuelMi = Number(vehicle?.starting_fuel_mi);
    if (!Number.isFinite(fuelMi)) fuelMi = tankRangeMi;

    setStatus('playing');
    setCurrentBeat(null);
    setTankFraction(clamp01(fuelMi / tankRangeMi));

    // Dwell is derived from (budget / stop_count); travel gets whatever's
    // left, distributed across legs proportional to each leg's own
    // distance so a long leg doesn't feel rushed relative to a short one.
    const dwellMs = computeDwellMs(stops.length);
    const reservedDwellMs = dwellMs * stops.length;
    const travelBudgetMs = Math.max(TOTAL_BUDGET_MS - reservedDwellMs, MIN_TRAVEL_BUDGET_MS);
    const legDistances = legs.map((leg) => Number(leg.distance_mi) || 0);
    const totalDistance = legDistances.reduce((sum, d) => sum + d, 0) || 1;
    const travelMsForLeg = legDistances.map((d) =>
      Math.max(MIN_LEG_MS, (d / totalDistance) * travelBudgetMs)
    );

    const startPoint = locationTuple(data.start);
    let previousPoint = startPoint;

    if (startPoint) {
      await moveCamera(
        map,
        { center: startPoint, zoom: TRAVEL_ZOOM, pitch: TRAVEL_PITCH, bearing: 0 },
        reduced ? 0 : INITIAL_FLY_MS,
        reduced
      );
    }
    if (!isCurrent()) return;

    let prevStopMi = 0;

    for (let i = 0; i < stops.length; i += 1) {
      const stop = stops[i];
      const leg = legs[i];
      const stopPoint = locationTuple(stop.location);
      const legMs = travelMsForLeg[i] ?? MIN_LEG_MS;

      if (stopPoint) {
        const bearing = previousPoint ? computeBearing(previousPoint, stopPoint) : 0;
        await moveCamera(map, { center: stopPoint, zoom: TRAVEL_ZOOM, pitch: TRAVEL_PITCH, bearing }, legMs, reduced);
        previousPoint = stopPoint;
      }
      if (!isCurrent()) return;

      // Fuel consumed reaching this stop -- the callout's "fuel
      // remaining" is this value, BEFORE this stop's own purchase (if
      // any) is added back below.
      fuelMi = Math.max(0, fuelMi - (Number(leg?.distance_mi) || 0));
      const fuelRemainingMi = fuelMi;
      setTankFraction(clamp01(fuelMi / tankRangeMi));

      // Ease UP and OUT: only altitude (zoom) changes here, pitch
      // stays at STOP_PITCH (== TRAVEL_PITCH) so this beat never fights
      // the map's own controlled pitch prop.
      if (stopPoint) {
        await moveCamera(
          map,
          { center: stopPoint, zoom: STOP_ZOOM, pitch: STOP_PITCH, bearing: 0 },
          reduced ? 0 : EASE_OUT_MS,
          reduced
        );
      }
      if (!isCurrent()) return;

      const thisStopMi = Number(stop.distance_from_start_mi);
      const stopMiSafe = Number.isFinite(thisStopMi) ? thisStopMi : prevStopMi;
      // Matches the backend's own skipped rule, re-derived frontend-side:
      // every in-corridor candidate strictly between the previous and
      // this stop's own mile marker was passed over in favor of this one.
      const skippedCandidates = candidates.filter((c) => {
        const d = Number(c.distance_from_start_mi);
        return Number.isFinite(d) && d > prevStopMi && d < stopMiSafe;
      });

      const beat: ChaseCamBeat = {
        index: i,
        stop,
        fuelRemainingMi,
        gallonsToppedUp: stop.gallons,
        pricePaid: stop.cost,
        skippedCount: stop.rationale.skipped_count,
        skippedAvgPrice: stop.rationale.skipped_avg_price,
        skippedCandidates,
      };
      setCurrentBeat(beat);

      const gallons = Number(stop.gallons) || 0;
      const refillWaitMs = Math.min(dwellMs * 0.35, 500);
      await wait(reduced ? Math.min(200, dwellMs) : refillWaitMs);
      if (!isCurrent()) return;

      // Refill -- the tank gauge's own CSS transition supplies the
      // visible "refilling" motion (TankGauge.tsx).
      fuelMi = Math.min(tankRangeMi, fuelMi + gallons * mpg);
      setTankFraction(clamp01(fuelMi / tankRangeMi));

      await wait(Math.max(0, dwellMs - refillWaitMs));
      if (!isCurrent()) return;

      prevStopMi = stopMiSafe;
    }

    const finishPoint = locationTuple(data.finish);
    const finalLeg = legs[legs.length - 1];
    const finalLegMs = travelMsForLeg[travelMsForLeg.length - 1] ?? MIN_LEG_MS;

    if (finishPoint) {
      const bearing = previousPoint ? computeBearing(previousPoint, finishPoint) : 0;
      await moveCamera(map, { center: finishPoint, zoom: FINISH_ZOOM, pitch: TRAVEL_PITCH, bearing }, finalLegMs, reduced);
    }
    if (!isCurrent()) return;

    fuelMi = Math.max(0, fuelMi - (Number(finalLeg?.distance_mi) || 0));
    setTankFraction(clamp01(fuelMi / tankRangeMi));
    setCurrentBeat(null);
    // Natural completion ends in the savings finale -- unlike
    // `skip()`, which returns straight to the static view.
    setStatus('finished');
  }, [map, data, status]);

  return useMemo(
    () => ({ status, currentBeat, tankFraction, canPlay, play, skip, dismiss }),
    [status, currentBeat, tankFraction, canPlay, play, skip, dismiss]
  );
}
