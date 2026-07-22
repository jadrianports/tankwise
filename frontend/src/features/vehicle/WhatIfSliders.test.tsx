import { render, screen, cleanup, fireEvent } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';

import WhatIfSliders from './WhatIfSliders';
import type { VehicleProfileRequest } from '../../types/routeContract';

// This file's vite config runs without vitest's `globals` option, so
// testing-library's auto-cleanup detection never fires -- each render
// must be torn down explicitly between tests.
afterEach(cleanup);

const VEHICLE: VehicleProfileRequest = { mpg: 6.5, tank_range_mi: 1050, starting_fuel: 0.8 };

test('all three sliders render, each reachable by its accessible name and reflecting the supplied profile', () => {
  render(<WhatIfSliders vehicle={VEHICLE} onChange={() => {}} />);

  const mpgSlider = screen.getByRole('slider', { name: 'Miles per gallon' }) as HTMLInputElement;
  const tankSlider = screen.getByRole('slider', { name: 'Tank range in miles' }) as HTMLInputElement;
  const fuelSlider = screen.getByRole('slider', { name: 'Starting fuel fraction' }) as HTMLInputElement;

  expect(mpgSlider.value).toBe(String(VEHICLE.mpg));
  expect(tankSlider.value).toBe(String(VEHICLE.tank_range_mi));
  expect(fuelSlider.value).toBe(String(VEHICLE.starting_fuel));
});

test('the three label lines render the formatted values derived from the profile', () => {
  render(<WhatIfSliders vehicle={VEHICLE} onChange={() => {}} />);

  expect(screen.getByText(`MPG: ${VEHICLE.mpg}`)).toBeInTheDocument();
  expect(screen.getByText(`Tank range: ${VEHICLE.tank_range_mi} mi`)).toBeInTheDocument();
});

test('the starting-fuel label renders a whole-number percentage derived from the fraction, not the raw fraction', () => {
  render(<WhatIfSliders vehicle={VEHICLE} onChange={() => {}} />);

  expect(screen.getByText('Starting fuel: 80%')).toBeInTheDocument();
});

test('passing the disabled prop renders all three sliders disabled', () => {
  render(<WhatIfSliders vehicle={VEHICLE} disabled onChange={vi.fn()} />);

  expect(screen.getByRole('slider', { name: 'Miles per gallon' })).toBeDisabled();
  expect(screen.getByRole('slider', { name: 'Tank range in miles' })).toBeDisabled();
  expect(screen.getByRole('slider', { name: 'Starting fuel fraction' })).toBeDisabled();
});

// A change event on the accessible slider input (not a simulated pointer
// drag -- this is the standard/no-extra-dependency way to exercise MUI's
// Slider onChange wiring) reaches each handler's merge-into-profile logic.
test('changing each slider merges the new field into the profile passed to onChange', () => {
  const onChange = vi.fn();
  render(<WhatIfSliders vehicle={VEHICLE} onChange={onChange} />);

  fireEvent.change(screen.getByRole('slider', { name: 'Miles per gallon' }), { target: { value: '10' } });
  expect(onChange).toHaveBeenCalledWith({ ...VEHICLE, mpg: 10 });

  fireEvent.change(screen.getByRole('slider', { name: 'Tank range in miles' }), { target: { value: '600' } });
  expect(onChange).toHaveBeenCalledWith({ ...VEHICLE, tank_range_mi: 600 });

  fireEvent.change(screen.getByRole('slider', { name: 'Starting fuel fraction' }), { target: { value: '0.5' } });
  expect(onChange).toHaveBeenCalledWith({ ...VEHICLE, starting_fuel: 0.5 });
});
