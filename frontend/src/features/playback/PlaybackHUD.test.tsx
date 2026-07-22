import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';

import PlaybackHUD from './PlaybackHUD';
import type { ChaseCamBeat } from './useChaseCam';

// This file's vite config runs without vitest's `globals` option, so
// testing-library's auto-cleanup detection never fires -- each render
// must be torn down explicitly between tests.
afterEach(cleanup);

// Minimal literal cast through `unknown`, carrying only the fields the
// component reads -- a type-only import, erased at compile time, so no
// runtime dependency on the excluded chase-cam module is introduced.
const BASE_BEAT = {
  index: 0,
  stop: { name: 'Pilot Travel Center' },
  fuelRemainingMi: 120,
  gallonsToppedUp: '45.5',
  pricePaid: '150.25',
  skippedCount: 0,
  skippedAvgPrice: null,
} as unknown as ChaseCamBeat;

test('renders the idle on-the-road copy when the beat is null', () => {
  render(<PlaybackHUD currentBeat={null} tankFraction={1} onSkip={() => {}} />);
  expect(screen.getByText('On the road…')).toBeInTheDocument();
});

test('renders the one-based stop heading and formatted detail line for a populated beat', () => {
  render(<PlaybackHUD currentBeat={BASE_BEAT} tankFraction={0.5} onSkip={() => {}} />);

  expect(screen.getByText('Stop 1: Pilot Travel Center')).toBeInTheDocument();
  expect(
    screen.getByText('120 mi of range left · 45.50 gal topped up · $150.25 paid')
  ).toBeInTheDocument();
});

test('omits the passed-stations sentence when the skip count is 0', () => {
  render(<PlaybackHUD currentBeat={BASE_BEAT} tankFraction={0.5} onSkip={() => {}} />);
  expect(screen.queryByText(/^Passed/)).not.toBeInTheDocument();
});

test('renders the passed-stations sentence, singular, at a skip count of 1', () => {
  const beat = { ...BASE_BEAT, skippedCount: 1 } as unknown as ChaseCamBeat;
  render(<PlaybackHUD currentBeat={beat} tankFraction={0.5} onSkip={() => {}} />);
  expect(screen.getByText('Passed 1 station.')).toBeInTheDocument();
});

test('renders the passed-stations sentence, plural, at a skip count above 1', () => {
  const beat = { ...BASE_BEAT, skippedCount: 3 } as unknown as ChaseCamBeat;
  render(<PlaybackHUD currentBeat={beat} tankFraction={0.5} onSkip={() => {}} />);
  expect(screen.getByText('Passed 3 stations.')).toBeInTheDocument();
});

test('invokes the skip callback when the Skip button is clicked', () => {
  const onSkip = vi.fn();
  render(<PlaybackHUD currentBeat={BASE_BEAT} tankFraction={0.5} onSkip={onSkip} />);

  fireEvent.click(screen.getByRole('button', { name: 'Skip' }));

  expect(onSkip).toHaveBeenCalledTimes(1);
});
