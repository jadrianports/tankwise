import { renderHook, act } from '@testing-library/react';
import { vi, expect, test, beforeEach, afterEach } from 'vitest';
import { createElement } from 'react';
import type { ReactNode } from 'react';

import { useDebouncedResolve } from './useDebouncedResolve';
import { RoutePlanContext } from '../../context/RoutePlanContext';
import type { RoutePlanContextValue } from '../../context/RoutePlanContext';
import type { VehicleProfileRequest } from '../../types/routeContract';

const resolveVehicle = vi.fn<(vehicle: VehicleProfileRequest) => void>();
const retry = vi.fn();

const BASE_CONTEXT: RoutePlanContextValue = {
  status: 'idle',
  data: null,
  error: null,
  solve: async () => {},
  retry,
  focusStop: () => {},
  resolveVehicle,
};

beforeEach(() => {
  vi.useFakeTimers();
  resolveVehicle.mockReset();
  retry.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

function renderWithContext(overrides: Partial<RoutePlanContextValue> = {}) {
  const value: RoutePlanContextValue = { ...BASE_CONTEXT, ...overrides };
  return renderHook(() => useDebouncedResolve(), {
    wrapper: ({ children }: { children: ReactNode }) =>
      createElement(RoutePlanContext.Provider, { value }, children),
  });
}

const VEHICLE_A: VehicleProfileRequest = { mpg: 6.5, tank_range_mi: 1050, starting_fuel: 1 };
const VEHICLE_B: VehicleProfileRequest = { mpg: 8, tank_range_mi: 700, starting_fuel: 0.8 };
const VEHICLE_C: VehicleProfileRequest = { mpg: 32, tank_range_mi: 450, starting_fuel: 0.5 };

test('three rapid vehicle changes inside the debounce window resolve exactly once, with the last value', () => {
  const { result } = renderWithContext();

  act(() => {
    result.current.onVehicleChange(VEHICLE_A);
    result.current.onVehicleChange(VEHICLE_B);
    result.current.onVehicleChange(VEHICLE_C);
  });
  act(() => {
    vi.advanceTimersByTime(500);
  });

  expect(resolveVehicle).toHaveBeenCalledTimes(1);
  expect(resolveVehicle).toHaveBeenCalledWith(VEHICLE_C);
});

test('advancing less than the debounce window after a single change resolves nothing yet', () => {
  const { result } = renderWithContext();

  act(() => {
    result.current.onVehicleChange(VEHICLE_A);
  });
  act(() => {
    vi.advanceTimersByTime(499);
  });

  expect(resolveVehicle).not.toHaveBeenCalled();
});

test('a rate-limited status with a numeric retry-after seeds the countdown at its ceiling and reports paused', () => {
  const { result } = renderWithContext({
    status: 'rate_limited',
    error: { code: 'rate_limited', message: 'Catching up', retryAfterS: 4.2 },
  });

  expect(result.current.retryCountdown).toBe(5);
  expect(result.current.isPaused).toBe(true);
});

test('advancing one second decrements the countdown by one', () => {
  const { result } = renderWithContext({
    status: 'rate_limited',
    error: { code: 'rate_limited', message: 'Catching up', retryAfterS: 4.2 },
  });
  expect(result.current.retryCountdown).toBe(5);

  act(() => {
    vi.advanceTimersByTime(1000);
  });

  expect(result.current.retryCountdown).toBe(4);
});

test('the countdown reaching zero with a pending vehicle flushes it and does not retry', () => {
  const { result } = renderWithContext({
    status: 'rate_limited',
    error: { code: 'rate_limited', message: 'Catching up', retryAfterS: 1 },
  });

  act(() => {
    // Recorded while paused -- onVehicleChange schedules nothing but still
    // remembers the last value the user landed on during the cooldown.
    result.current.onVehicleChange(VEHICLE_B);
  });

  act(() => {
    vi.advanceTimersByTime(1000);
  });

  expect(resolveVehicle).toHaveBeenCalledTimes(1);
  expect(resolveVehicle).toHaveBeenCalledWith(VEHICLE_B);
  expect(retry).not.toHaveBeenCalled();
});

test('the countdown reaching zero with nothing pending retries and does not flush a vehicle', () => {
  renderWithContext({
    status: 'rate_limited',
    error: { code: 'rate_limited', message: 'Catching up', retryAfterS: 1 },
  });

  act(() => {
    vi.advanceTimersByTime(1000);
  });

  expect(retry).toHaveBeenCalledTimes(1);
  expect(resolveVehicle).not.toHaveBeenCalled();
});

test('onVehicleChange while paused schedules nothing, even past the debounce window', () => {
  const { result } = renderWithContext({
    status: 'rate_limited',
    error: { code: 'rate_limited', message: 'Catching up', retryAfterS: 30 },
  });

  expect(result.current.isPaused).toBe(true);

  act(() => {
    result.current.onVehicleChange(VEHICLE_A);
  });
  act(() => {
    vi.advanceTimersByTime(500);
  });

  expect(resolveVehicle).not.toHaveBeenCalled();
});
