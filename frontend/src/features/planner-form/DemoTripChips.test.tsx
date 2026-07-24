import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { expect, test, afterEach } from 'vitest';

import DemoTripChips from './DemoTripChips';
import { DEMO_TRIPS } from '../../constants/presets';
import type { DemoTrip } from '../../constants/presets';

afterEach(() => {
  cleanup();
});

test('clicking the multi-stop demo chip forwards its waypoints through onSelect', () => {
  const multiStopTrip = DEMO_TRIPS.find((trip) => trip.waypoints && trip.waypoints.length > 0);
  expect(multiStopTrip).toBeDefined();

  let selected: DemoTrip | null = null;
  render(<DemoTripChips isLoading={false} onSelect={(trip) => (selected = trip)} />);

  fireEvent.click(screen.getByText(multiStopTrip!.label));

  expect(selected).not.toBeNull();
  expect((selected as unknown as DemoTrip).waypoints).toEqual(multiStopTrip!.waypoints);
});

test('clicking an original A-to-B demo chip forwards a trip with no waypoints', () => {
  const abTrip = DEMO_TRIPS.find((trip) => !trip.waypoints);
  expect(abTrip).toBeDefined();

  let selected: DemoTrip | null = null;
  render(<DemoTripChips isLoading={false} onSelect={(trip) => (selected = trip)} />);

  fireEvent.click(screen.getByText(abTrip!.label));

  expect(selected).not.toBeNull();
  expect((selected as unknown as DemoTrip).waypoints).toBeUndefined();
});
