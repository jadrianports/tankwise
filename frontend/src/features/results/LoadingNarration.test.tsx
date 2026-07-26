import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';

import LoadingNarration from './LoadingNarration';
import { resetBootSignal } from '../../api/routeClient';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  // routeClient's boot signal is module-level state, shared across every
  // test in this file -- reset it so an earlier test's `true` never leaks
  // into a later one that expects the default "no signal" state.
  resetBootSignal();
});

test('at mount, the narration reads the solving sentence', () => {
  vi.useFakeTimers();
  render(<LoadingNarration />);

  expect(screen.getByText(/solving your route/i)).toBeInTheDocument();
});

test('at 12000ms with no boot signal, the narration is honest and indeterminate -- neither an active-solving claim nor a wake claim', () => {
  vi.useFakeTimers();
  render(<LoadingNarration />);

  act(() => {
    vi.advanceTimersByTime(12000);
  });

  const narration = screen.getByText(/still working/i);
  expect(narration).toHaveTextContent(/free-tier/i);
  expect(narration).toHaveTextContent(/asleep/i);
  expect(narration).not.toHaveTextContent(/still solving/i);
  expect(narration).not.toHaveTextContent(/pricing stops/i);
});

test('at 18000ms with no boot signal, the narration escalates to the waking sentence', () => {
  vi.useFakeTimers();
  render(<LoadingNarration />);

  act(() => {
    vi.advanceTimersByTime(18000);
  });

  expect(screen.getByText(/waking up the server/i)).toBeInTheDocument();
});

test('at 18000ms the spinner is still present alongside the waking narration', () => {
  vi.useFakeTimers();
  render(<LoadingNarration />);

  act(() => {
    vi.advanceTimersByTime(18000);
  });

  expect(screen.getByRole('progressbar')).toBeInTheDocument();
});
