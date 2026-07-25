import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';

import ColdStartModal from './ColdStartModal';
import { markServerNotAnswering, resetBootSignal } from '../api/routeClient';
import { RoutePlanContext } from '../context/RoutePlanContext';
import type { RoutePlanContextValue } from '../context/RoutePlanContext';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  // routeClient's boot signal is module-level state, shared across every
  // test in this file -- reset it so an earlier test's `true` never
  // leaks into a later one that expects the default "no signal" state.
  resetBootSignal();
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

test('rendered with isLoading true, nothing appears for the first 4000ms of fake-timer time, even with the boot signal already fired', () => {
  markServerNotAnswering();
  vi.useFakeTimers();
  renderWithStatus('loading');

  act(() => {
    vi.advanceTimersByTime(3999);
  });

  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

test('past 4000ms with NO boot signal, the dialog never appears -- a slow-but-answering request is not a server wake-up', () => {
  vi.useFakeTimers();
  renderWithStatus('loading');

  act(() => {
    vi.advanceTimersByTime(20000);
  });

  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

test('after 4000ms WITH the genuine boot signal, an accessible dialog appears with progress, honest copy, and a Close control', () => {
  vi.useFakeTimers();
  renderWithStatus('loading');
  markServerNotAnswering();

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

test('the boot signal firing AFTER the 4000ms threshold still opens the dialog (fully reactive, no new timer)', () => {
  vi.useFakeTimers();
  renderWithStatus('loading');

  act(() => {
    vi.advanceTimersByTime(4000);
  });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

  act(() => {
    markServerNotAnswering();
  });

  expect(screen.getByRole('dialog', { name: /waking up the server/i })).toBeInTheDocument();
});

test('clicking the Close control closes the dialog and it does not reopen while the same load is still in flight', () => {
  vi.useFakeTimers();
  renderWithStatus('loading');
  markServerNotAnswering();

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
  markServerNotAnswering();
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
