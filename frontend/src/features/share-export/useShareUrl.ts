// Shareable trip URLs (UX-04, D-27/D-28). Reuses tripState.ts's canonical
// encode/decode UNCHANGED (09-05's "one trip-state shape" -- this file is
// not a second serialization format, just the reader/writer for
// window.location.search, D-03's "no router" boundary).
import { useEffect, useMemo, useRef } from 'react';

import { decodeTripState, encodeTripState, type TripState } from './tripState';
import { HERO_VEHICLE_PRESET_ID, VEHICLE_PRESETS } from '../../constants/presets';
import type { RouteResponse, VehicleProfileRequest } from '../../types/routeContract';

const HERO_VEHICLE = VEHICLE_PRESETS.find((preset) => preset.id === HERO_VEHICLE_PRESET_ID)!.vehicle;

type SubmitFn = (start: string, finish: string, vehicle?: VehicleProfileRequest) => Promise<void>;

// TripState.vehicle is a preset id string (09-05's canonical shape, D-38's
// only vehicle identifier that exists client-side) -- resolve it back to a
// real request body, falling back to the hero preset for an unknown/absent
// id rather than failing the whole link open.
export function vehicleForPresetId(id: string): VehicleProfileRequest {
  return VEHICLE_PRESETS.find((preset) => preset.id === id)?.vehicle ?? HERO_VEHICLE;
}

// The inverse: given the RouteResponse's echoed vehicle (full-precision
// Decimal strings, D-36 presets are exact floats), find the preset that
// produced it. A hand-tuned WhatIfSliders value that doesn't land on any
// preset falls back to the hero preset id -- the same approximation
// useRecentTrips.ts already accepts for the identical reason: the
// canonical TripState shape stores a preset id, not raw slider values.
export function presetIdForVehicle(vehicle: RouteResponse['vehicle']): string {
  if (!vehicle) return HERO_VEHICLE_PRESET_ID;
  const mpg = Number(vehicle.mpg);
  const tankRangeMi = Number(vehicle.tank_range_mi);
  const match = VEHICLE_PRESETS.find(
    (preset) => preset.vehicle.mpg === mpg && preset.vehicle.tank_range_mi === tankRangeMi
  );
  return match?.id ?? HERO_VEHICLE_PRESET_ID;
}

// Builds the absolute shareable URL for the last solved plan. D-27: inputs
// only -- start/finish coordinates and the vehicle profile, never the
// solved plan itself (T-09-17: no secrets, no solved data in the URL).
// Labels are intentionally omitted (tripState.ts's own encode/decode
// already falls back to the coordinate string as the label when absent --
// this file has no access to the human-readable label PlannerFormSection
// tracks locally, only the resolved coordinates the backend echoed back).
export function buildShareTripState(data: RouteResponse | null): TripState | null {
  if (!data?.start || !data?.finish) return null;
  if (data.start.latitude === null || data.start.longitude === null) return null;
  if (data.finish.latitude === null || data.finish.longitude === null) return null;

  return {
    start: `${data.start.latitude},${data.start.longitude}`,
    finish: `${data.finish.latitude},${data.finish.longitude}`,
    startLabel: '',
    finishLabel: '',
    vehicle: presetIdForVehicle(data.vehicle),
  };
}

export function buildShareUrl(data: RouteResponse | null): string | null {
  const trip = buildShareTripState(data);
  if (!trip) return null;
  const qs = encodeTripState(trip).toString();
  return `${window.location.origin}${window.location.pathname}?${qs}`;
}

export interface UseShareUrlResult {
  // Null until a plan has been solved -- there is nothing to share yet.
  shareUrl: string | null;
}

// On first mount, decodes window.location.search (D-03: read/write the
// query string directly, no router) via tripState's canonical
// decodeTripState; a valid trip auto-solves immediately (D-28) with its
// encoded vehicle profile resolved back to a request body, so the loading
// narration and the correct, deterministic result appear on the very first
// render -- no second, self-correcting re-solve.
export function useShareUrl(submit: SubmitFn, data: RouteResponse | null): UseShareUrlResult {
  const firedRef = useRef(false);

  useEffect(() => {
    if (firedRef.current) return;
    firedRef.current = true;
    const trip = decodeTripState(window.location.search);
    if (!trip) return;
    void submit(trip.start, trip.finish, vehicleForPresetId(trip.vehicle));
  }, [submit]);

  const shareUrl = useMemo(() => buildShareUrl(data), [data]);

  return { shareUrl };
}
