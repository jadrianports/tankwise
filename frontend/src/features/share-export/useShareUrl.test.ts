import { renderHook, waitFor } from '@testing-library/react';
import { beforeEach, expect, test, vi } from 'vitest';

import { buildShareTripState, buildShareUrl, presetIdForVehicle, useShareUrl, vehicleForPresetId } from './useShareUrl';
import { HERO_VEHICLE_PRESET_ID, VEHICLE_PRESETS } from '../../constants/presets';
import type { RouteResponse, VehicleEcho } from '../../types/routeContract';

const RV_PRESET = VEHICLE_PRESETS.find((preset) => preset.id === 'rv')!;
const HERO_PRESET = VEHICLE_PRESETS.find((preset) => preset.id === HERO_VEHICLE_PRESET_ID)!;

const RV_VEHICLE_ECHO: VehicleEcho = {
  mpg: String(RV_PRESET.vehicle.mpg),
  tank_range_mi: String(RV_PRESET.vehicle.tank_range_mi),
  starting_fuel: String(RV_PRESET.vehicle.starting_fuel),
  starting_fuel_mi: String(RV_PRESET.vehicle.tank_range_mi),
};

const ROUTE_WITH_RV_VEHICLE = {
  start: { latitude: '34.0522', longitude: '-118.2437' },
  finish: { latitude: '40.7128', longitude: '-74.0060' },
  vehicle: RV_VEHICLE_ECHO,
} as unknown as RouteResponse;

beforeEach(() => {
  window.history.pushState({}, '', '/');
});

test('vehicleForPresetId returns the matching vehicle profile for a known preset id', () => {
  expect(vehicleForPresetId('rv')).toEqual(RV_PRESET.vehicle);
});

test('vehicleForPresetId falls back to the hero vehicle profile for an unknown preset id', () => {
  expect(vehicleForPresetId('not-a-real-preset')).toEqual(HERO_PRESET.vehicle);
});

test('presetIdForVehicle returns the preset id whose profile matches a given vehicle', () => {
  expect(presetIdForVehicle(RV_VEHICLE_ECHO)).toBe('rv');
});

test('presetIdForVehicle falls back to the hero preset id when the vehicle is absent', () => {
  expect(presetIdForVehicle(null)).toBe(HERO_VEHICLE_PRESET_ID);
});

test('presetIdForVehicle falls back to the hero preset id when no preset matches', () => {
  const unmatched: VehicleEcho = { mpg: '11', tank_range_mi: '999', starting_fuel: '1', starting_fuel_mi: '999' };
  expect(presetIdForVehicle(unmatched)).toBe(HERO_VEHICLE_PRESET_ID);
});

test('buildShareTripState returns null for null input', () => {
  expect(buildShareTripState(null)).toBeNull();
});

test('buildShareTripState returns null when the resolved coordinates are missing', () => {
  const data = {
    start: { latitude: null, longitude: null },
    finish: { latitude: '1', longitude: '2' },
    vehicle: null,
  } as unknown as RouteResponse;
  expect(buildShareTripState(data)).toBeNull();
});

test('buildShareTripState returns a fully populated TripState for a real route response', () => {
  expect(buildShareTripState(ROUTE_WITH_RV_VEHICLE)).toEqual({
    start: '34.0522,-118.2437',
    finish: '40.7128,-74.0060',
    startLabel: '',
    finishLabel: '',
    vehicle: 'rv',
  });
});

test('buildShareUrl returns null for null input', () => {
  expect(buildShareUrl(null)).toBeNull();
});

test('buildShareUrl returns an absolute URL containing the encoded trip parameters', () => {
  const url = buildShareUrl(ROUTE_WITH_RV_VEHICLE);
  expect(url).toContain(window.location.origin);
  expect(url).toContain('start=34.0522%2C-118.2437');
  expect(url).toContain('finish=40.7128%2C-74.0060');
  expect(url).toContain('vehicle=rv');
});

test('useShareUrl exposes a null shareUrl when no route data is present', () => {
  const submit = vi.fn().mockResolvedValue(undefined);
  const { result } = renderHook(() => useShareUrl(submit, null));
  expect(result.current.shareUrl).toBeNull();
});

test('useShareUrl exposes a share URL derived from the supplied route data once data is present', () => {
  const submit = vi.fn().mockResolvedValue(undefined);
  const { result } = renderHook(() => useShareUrl(submit, ROUTE_WITH_RV_VEHICLE));
  expect(result.current.shareUrl).toContain('start=34.0522%2C-118.2437');
});

test('useShareUrl calls submit on mount when the current query string decodes to a valid trip', async () => {
  window.history.pushState({}, '', '/?start=34.0522%2C-118.2437&finish=40.7128%2C-74.0060&vehicle=rv');
  const submit = vi.fn().mockResolvedValue(undefined);
  renderHook(() => useShareUrl(submit, null));
  await waitFor(() => expect(submit).toHaveBeenCalledOnce());
  expect(submit).toHaveBeenCalledWith('34.0522,-118.2437', '40.7128,-74.0060', RV_PRESET.vehicle);
});

test('useShareUrl does not call submit on mount when the query string is empty', () => {
  const submit = vi.fn().mockResolvedValue(undefined);
  renderHook(() => useShareUrl(submit, null));
  expect(submit).not.toHaveBeenCalled();
});
