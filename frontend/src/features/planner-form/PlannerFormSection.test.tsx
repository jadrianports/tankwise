import { render, screen, fireEvent, cleanup, within, act } from '@testing-library/react';
import { expect, test, afterEach, vi } from 'vitest';
import type { ReactNode } from 'react';

import PlannerFormSection from './PlannerFormSection';
import { RoutePlanContext, type RoutePlanContextValue } from '../../context/RoutePlanContext';
import { requestLoadTrip } from '../share-export/tripState';

const RECENT_TRIPS_STORAGE_KEY = 'spotter.recentTrips.v1';

function readRecentTrips(): Array<{ start: string; finish: string; waypoints?: string[] }> {
  const raw = window.localStorage.getItem(RECENT_TRIPS_STORAGE_KEY);
  return raw ? JSON.parse(raw) : [];
}

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  vi.restoreAllMocks();
});

function renderPlanner(overrides: Partial<RoutePlanContextValue> = {}) {
  const solve = overrides.solve ?? vi.fn().mockResolvedValue(undefined);
  const value: RoutePlanContextValue = {
    status: 'idle',
    data: null,
    error: null,
    retry: vi.fn(),
    focusStop: vi.fn(),
    resolveVehicle: vi.fn(),
    elevationProfile: null,
    setHoveredElevationDistanceMi: vi.fn(),
    ...overrides,
    solve,
  };
  function Wrapper({ children }: { children: ReactNode }) {
    return <RoutePlanContext.Provider value={value}>{children}</RoutePlanContext.Provider>;
  }
  render(<PlannerFormSection />, { wrapper: Wrapper });
  return { solve };
}

test('planner opens with only Start and Finish, no middle stops', () => {
  renderPlanner();
  expect(screen.getByLabelText('Start')).toBeInTheDocument();
  expect(screen.getByLabelText('Finish')).toBeInTheDocument();
  expect(screen.queryByLabelText(/^Stop /)).not.toBeInTheDocument();
});

test('"+ Add stop" inserts an intermediate row; remove deletes it', () => {
  renderPlanner();

  fireEvent.click(screen.getByRole('button', { name: 'Add stop' }));
  expect(screen.getByLabelText('Stop B')).toBeInTheDocument();

  fireEvent.click(screen.getByRole('button', { name: 'Remove stop B' }));
  expect(screen.queryByLabelText('Stop B')).not.toBeInTheDocument();
});

test('Start and Finish are never removable; swap still swaps only the two endpoints', () => {
  renderPlanner();

  // No remove control exists for Start/Finish -- only "Remove stop X" for
  // middle stops, and none exist yet.
  expect(screen.queryByRole('button', { name: /remove/i })).not.toBeInTheDocument();

  fireEvent.change(screen.getByLabelText('Start'), { target: { value: 'Origin City' } });
  fireEvent.blur(screen.getByLabelText('Start'));
  fireEvent.change(screen.getByLabelText('Finish'), { target: { value: 'Destination City' } });
  fireEvent.blur(screen.getByLabelText('Finish'));

  fireEvent.click(screen.getByRole('button', { name: 'Swap start and finish' }));

  expect(screen.getByLabelText('Start')).toHaveValue('Destination City');
  expect(screen.getByLabelText('Finish')).toHaveValue('Origin City');
});

test('at 10 total stops, "+ Add stop" is disabled and helper text is visible', () => {
  renderPlanner();

  const addButton = screen.getByRole('button', { name: 'Add stop' });
  // 2 (Start/Finish) + 8 middle stops = 10 total (MAX_STOPS).
  for (let i = 0; i < 8; i += 1) {
    fireEvent.click(addButton);
  }

  expect(addButton).toBeDisabled();
  expect(screen.getByText('Maximum 10 stops.')).toBeInTheDocument();

  const stopLabels = screen.getAllByLabelText(/^Stop /).map((el) => (el as HTMLInputElement).labels?.[0]?.textContent);
  expect(stopLabels).toHaveLength(8);
});

test('the up/down keyboard-accessible reorder equivalent moves a middle stop without a pointer', () => {
  renderPlanner();

  fireEvent.click(screen.getByRole('button', { name: 'Add stop' }));
  fireEvent.click(screen.getByRole('button', { name: 'Add stop' }));

  fireEvent.change(screen.getByLabelText('Stop B'), { target: { value: 'B Address' } });
  fireEvent.blur(screen.getByLabelText('Stop B'));
  fireEvent.change(screen.getByLabelText('Stop C'), { target: { value: 'C Address' } });
  fireEvent.blur(screen.getByLabelText('Stop C'));

  // First row (B) has no "up" (list boundary); last row (C) has no "down".
  expect(screen.getByRole('button', { name: 'Move stop B up' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Move stop C down' })).toBeDisabled();

  fireEvent.click(screen.getByRole('button', { name: 'Move stop B down' }));

  // After the move, position 1 (letter B) now holds the former "C Address".
  expect(screen.getByLabelText('Stop B')).toHaveValue('C Address');
  expect(screen.getByLabelText('Stop C')).toHaveValue('B Address');
});

test('an aria-live region announces a reorder referencing the stop letter', () => {
  renderPlanner();

  fireEvent.click(screen.getByRole('button', { name: 'Add stop' }));
  fireEvent.click(screen.getByRole('button', { name: 'Add stop' }));

  const liveRegion = screen.getByTestId('reorder-announcements');
  expect(liveRegion).toHaveTextContent('');

  fireEvent.click(screen.getByRole('button', { name: 'Move stop B down' }));

  expect(liveRegion).toHaveTextContent(/Stop B moved to position 2/);
});

test('middle stop rows render between Start and Finish and letter sequentially', () => {
  renderPlanner();

  fireEvent.click(screen.getByRole('button', { name: 'Add stop' }));
  fireEvent.click(screen.getByRole('button', { name: 'Add stop' }));

  expect(screen.getByLabelText('Stop B')).toBeInTheDocument();
  expect(screen.getByLabelText('Stop C')).toBeInTheDocument();

  const form = screen.getByLabelText('Start').closest('form');
  expect(form).not.toBeNull();
  const labels = within(form as HTMLElement)
    .getAllByRole('combobox')
    .map((el) => el.getAttribute('id'));
  // Sanity: 4 fields total in DOM order (Start, Stop B, Stop C, Finish).
  expect(labels).toHaveLength(4);
});

test('submit with two middle stops calls solve with the ordered waypoints and records them in recent trips', () => {
  const { solve } = renderPlanner();

  fireEvent.change(screen.getByLabelText('Start'), { target: { value: '34.0522,-118.2437' } });
  fireEvent.blur(screen.getByLabelText('Start'));
  fireEvent.change(screen.getByLabelText('Finish'), { target: { value: '41.8781,-87.6298' } });
  fireEvent.blur(screen.getByLabelText('Finish'));

  fireEvent.click(screen.getByRole('button', { name: 'Add stop' }));
  fireEvent.change(screen.getByLabelText('Stop B'), { target: { value: '39.7392,-104.9903' } });
  fireEvent.blur(screen.getByLabelText('Stop B'));
  fireEvent.click(screen.getByRole('button', { name: 'Add stop' }));
  fireEvent.change(screen.getByLabelText('Stop C'), { target: { value: '41.2565,-95.9345' } });
  fireEvent.blur(screen.getByLabelText('Stop C'));

  fireEvent.submit(screen.getByLabelText('Start').closest('form') as HTMLFormElement);

  expect(solve).toHaveBeenCalledWith('34.0522,-118.2437', '41.8781,-87.6298', [
    '39.7392,-104.9903',
    '41.2565,-95.9345',
  ]);

  const [savedTrip] = readRecentTrips();
  expect(savedTrip.waypoints).toEqual(['39.7392,-104.9903', '41.2565,-95.9345']);
});

test('with zero middle stops, submit behaves exactly like the pre-change 2-point flow', () => {
  const { solve } = renderPlanner();

  fireEvent.change(screen.getByLabelText('Start'), { target: { value: 'Origin' } });
  fireEvent.blur(screen.getByLabelText('Start'));
  fireEvent.change(screen.getByLabelText('Finish'), { target: { value: 'Destination' } });
  fireEvent.blur(screen.getByLabelText('Finish'));

  fireEvent.submit(screen.getByLabelText('Start').closest('form') as HTMLFormElement);

  expect(solve).toHaveBeenCalledWith('Origin', 'Destination', undefined);

  const [savedTrip] = readRecentTrips();
  expect(savedTrip.waypoints).toBeUndefined();
});

test('the LA -> Denver -> Chicago demo chip populates one middle stop and solves with the Denver coordinate', () => {
  const { solve } = renderPlanner();

  fireEvent.click(screen.getByText('Los Angeles → Denver → Chicago'));

  expect(solve).toHaveBeenCalledWith('34.0522,-118.2437', '41.8781,-87.6298', ['39.7392,-104.9903']);
  expect(screen.getByLabelText('Stop B')).toHaveValue('Denver');
  expect(screen.getByLabelText('Start')).toHaveValue('Los Angeles');
  expect(screen.getByLabelText('Finish')).toHaveValue('Chicago');

  const [savedTrip] = readRecentTrips();
  expect(savedTrip.waypoints).toEqual(['39.7392,-104.9903']);
});

test('an original A-to-B demo chip still solves with no waypoints (regression)', () => {
  const { solve } = renderPlanner();

  fireEvent.click(screen.getByText('Dallas → Seattle'));

  expect(solve).toHaveBeenCalledWith('32.7767,-96.7970', '47.6062,-122.3321', undefined);
  expect(screen.queryByLabelText(/^Stop /)).not.toBeInTheDocument();
});

test('an infeasible_route error with a leg_index highlights the two bounding stop rows and renders the named-leg callout', () => {
  renderPlanner({
    error: {
      code: 'infeasible_route',
      message: 'No feasible fuel plan.',
      detail: { leg_index: 1, leg_coords: [] },
    },
  });

  fireEvent.click(screen.getByRole('button', { name: 'Add stop' }));
  fireEvent.change(screen.getByLabelText('Stop B'), { target: { value: 'Denver' } });
  fireEvent.blur(screen.getByLabelText('Stop B'));

  // leg_index 1 == the second leg == B -> Finish (the last letter, "C"
  // with one middle stop) -- both bounding rows are highlighted, Start
  // (A) is not.
  expect(screen.getByTestId('stop-row-B')).toHaveAttribute('data-highlighted', 'true');
  expect(screen.getByTestId('stop-row-C')).toHaveAttribute('data-highlighted', 'true');
  expect(screen.getByTestId('stop-row-A')).not.toHaveAttribute('data-highlighted');

  expect(screen.getByRole('alert')).toHaveTextContent('Leg B→C');
});

test('a legacy infeasible error with no leg_index highlights nothing and renders no named-leg callout', () => {
  renderPlanner({
    error: {
      code: 'infeasible_route',
      message: 'No feasible fuel plan.',
      detail: { leg_index: null, leg_coords: null },
    },
  });

  expect(screen.getByTestId('stop-row-A')).not.toHaveAttribute('data-highlighted');
  expect(screen.getByTestId('stop-row-B')).not.toHaveAttribute('data-highlighted');
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

test('loading a multi-stop recent/shared trip restores its middle stops and re-solves with them', () => {
  const { solve } = renderPlanner();

  act(() => {
    requestLoadTrip({
      start: '34.0522,-118.2437',
      finish: '41.8781,-87.6298',
      startLabel: 'Los Angeles',
      finishLabel: 'Chicago',
      vehicle: 'semi-loaded',
      waypoints: ['39.7392,-104.9903'],
    });
  });

  expect(solve).toHaveBeenCalledWith('34.0522,-118.2437', '41.8781,-87.6298', ['39.7392,-104.9903']);
  expect(screen.getByLabelText('Stop B')).toHaveValue('39.7392,-104.9903');
});
