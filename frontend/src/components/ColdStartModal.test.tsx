import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';

import ColdStartModal from './ColdStartModal';
import { RoutePlanContext } from '../context/RoutePlanContext';
import type { RoutePlanContextValue } from '../context/RoutePlanContext';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const BASE_CONTEXT: RoutePlanContextValue = {
  status: 'idle',
  data: null,
  error: null,
  solve: async () => {},
  retry: () => {},
  focusStop: () => {},
  resolveVehicle: () => {},
  elevationProfile: null,
  setHoveredElevationDistanceMi: () => {},
};

function renderWithStatus(status: RoutePlanContextValue['status']) {
  return render(
    <RoutePlanContext.Provider value={{ ...BASE_CONTEXT, status }}>
      <ColdStartModal />
    </RoutePlanContext.Provider>
  );
}

test('rendered with isLoading false, the modal is closed and nothing appears in the document', () => {
  renderWithStatus('idle');
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

test('rendered with isLoading true, nothing appears for the first 4000ms of fake-timer time', () => {
  vi.useFakeTimers();
  renderWithStatus('loading');

  act(() => {
    vi.advanceTimersByTime(3999);
  });

  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

test('after 4000ms with isLoading true, an accessible dialog appears with progress, honest copy, and a Close control', () => {
  vi.useFakeTimers();
  renderWithStatus('loading');

  act(() => {
    vi.advanceTimersByTime(4000);
  });

  const dialog = screen.getByRole('dialog', { name: /waking up the server/i });
  expect(dialog).toBeInTheDocument();
  expect(screen.getByRole('progressbar')).toBeInTheDocument();
  expect(dialog).toHaveTextContent(/free tier/i);
  expect(dialog).toHaveTextContent(/30/);
  expect(dialog).toHaveTextContent(/60/);
  expect(screen.getByRole('button', { name: /close/i })).toBeInTheDocument();
});

test('clicking the Close control closes the dialog and it does not reopen while the same load is still in flight', () => {
  vi.useFakeTimers();
  renderWithStatus('loading');

  act(() => {
    vi.advanceTimersByTime(4000);
  });
  expect(screen.getByRole('dialog')).toBeInTheDocument();

  fireEvent.click(screen.getByRole('button', { name: /close/i }));
  act(() => {
    vi.advanceTimersByTime(500);
  });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

  act(() => {
    vi.advanceTimersByTime(20000);
  });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

test('when isLoading flips back to false the dialog is gone from the document', () => {
  vi.useFakeTimers();
  const { rerender } = render(
    <RoutePlanContext.Provider value={{ ...BASE_CONTEXT, status: 'loading' }}>
      <ColdStartModal />
    </RoutePlanContext.Provider>
  );

  act(() => {
    vi.advanceTimersByTime(4000);
  });
  expect(screen.getByRole('dialog')).toBeInTheDocument();

  rerender(
    <RoutePlanContext.Provider value={{ ...BASE_CONTEXT, status: 'success' }}>
      <ColdStartModal />
    </RoutePlanContext.Provider>
  );
  act(() => {
    vi.advanceTimersByTime(500);
  });

  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});
