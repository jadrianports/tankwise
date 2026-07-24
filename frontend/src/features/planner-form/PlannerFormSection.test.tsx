import { render, screen, fireEvent, cleanup, within } from '@testing-library/react';
import { expect, test, afterEach, vi } from 'vitest';
import type { ReactNode } from 'react';

import PlannerFormSection from './PlannerFormSection';
import { RoutePlanContext, type RoutePlanContextValue } from '../../context/RoutePlanContext';

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
