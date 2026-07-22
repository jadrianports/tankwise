import { render, screen, cleanup, fireEvent, act } from '@testing-library/react';
import { vi, expect, test, beforeEach, afterEach } from 'vitest';

import VehicleSection from './VehicleSection';
import { RoutePlanContext } from '../../context/RoutePlanContext';
import type { RoutePlanContextValue } from '../../context/RoutePlanContext';
import { VEHICLE_PRESETS } from '../../constants/presets';

const resolveVehicle = vi.fn();
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

// This file's vite config runs without vitest's `globals` option, so
// testing-library's auto-cleanup detection never fires -- each render
// must be torn down explicitly between tests.
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function renderSection(overrides: Partial<RoutePlanContextValue> = {}) {
  const value: RoutePlanContextValue = { ...BASE_CONTEXT, ...overrides };
  return render(
    <RoutePlanContext.Provider value={value}>
      <VehicleSection />
    </RoutePlanContext.Provider>
  );
}

const SEDAN_PRESET = VEHICLE_PRESETS.find((preset) => preset.id === 'sedan')!;

test('the hero preset chip renders selected on mount', () => {
  renderSection();

  const heroChip = screen.getByRole('button', { name: /Semi \(loaded\)/ });
  expect(heroChip.className).toMatch(/MuiChip-filled/);
});

test('clicking a different preset chip moves the selected state to that chip', () => {
  renderSection();

  const sedanChip = screen.getByRole('button', { name: /Sedan/ });
  fireEvent.click(sedanChip);

  expect(sedanChip.className).toMatch(/MuiChip-filled/);
  expect(screen.getByRole('button', { name: /Semi \(loaded\)/ }).className).toMatch(/MuiChip-outlined/);
});

test('clicking a preset chip and advancing past the debounce window resolves that preset\'s vehicle profile', () => {
  renderSection();

  fireEvent.click(screen.getByRole('button', { name: /Sedan/ }));

  act(() => {
    vi.advanceTimersByTime(500);
  });

  expect(resolveVehicle).toHaveBeenCalledWith(SEDAN_PRESET.vehicle);
});

test('a rate-limited context status renders the catching-up copy with the current countdown', () => {
  renderSection({
    status: 'rate_limited',
    error: { code: 'rate_limited', message: 'Catching up', retryAfterS: 7 },
  });

  expect(screen.getByRole('status')).toHaveTextContent('Catching up — retrying in 7s');
});

test('the preset chips render disabled while paused', () => {
  renderSection({
    status: 'rate_limited',
    error: { code: 'rate_limited', message: 'Catching up', retryAfterS: 7 },
  });

  for (const chip of screen.getAllByRole('button')) {
    expect(chip).toHaveAttribute('aria-disabled', 'true');
  }
});
