import { render, screen, cleanup } from '@testing-library/react';
import { afterEach, expect, test } from 'vitest';

import JustificationPopup from './JustificationPopup';
import type { FuelStop, PurchaseReason, Rationale } from '../../types/routeContract';

// This file's vite config runs without vitest's `globals` option, so
// testing-library's auto-cleanup detection never fires -- each render
// must be torn down explicitly between tests.
afterEach(cleanup);

function makeStop(rationale: Partial<Rationale> & { purchase_reason: PurchaseReason | null }): FuelStop {
  return {
    name: 'Test Stop',
    station_id: '1',
    location: { latitude: '41.8781', longitude: '-87.6298' },
    distance_from_start_mi: '200',
    price_per_gallon: '3.50',
    gallons: '40.00',
    cost: '140.00',
    rationale: {
      reason_target_station_id: null,
      reason_target_name: null,
      skipped_count: 0,
      skipped_avg_price: null,
      corridor_avg_price: null,
      price_percentile: null,
      bypassed_cheaper_count: 0,
      bypassed_saving_forgone: null,
      ...rationale,
    },
  };
}

test('renders the real bypass_cheaper_not_worth_stop sentence, naming the fill target', () => {
  const stop = makeStop({
    purchase_reason: 'bypass_cheaper_not_worth_stop',
    reason_target_name: 'CIRCLE K #4707605',
  });
  render(<JustificationPopup stop={stop} number={1} open onClose={() => {}} />);
  expect(screen.getByRole('dialog')).toBeInTheDocument();
  expect(
    screen.getByText(/filled up here and drove past circle k #4707605/i)
  ).toBeInTheDocument();
});

test('falls back gracefully when reason_target_name is null for the bypass reason', () => {
  const stop = makeStop({ purchase_reason: 'bypass_cheaper_not_worth_stop', reason_target_name: null });
  render(<JustificationPopup stop={stop} number={1} open onClose={() => {}} />);
  expect(
    screen.getByText(/filled up here and drove past a cheaper station up ahead/i)
  ).toBeInTheDocument();
});

test('renders a neutral fallback sentence, not a crash, for an unknown future reason value', () => {
  const stop = makeStop({ purchase_reason: 'some_future_reason' as PurchaseReason });
  expect(() =>
    render(<JustificationPopup stop={stop} number={1} open onClose={() => {}} />)
  ).not.toThrow();
  expect(screen.getByRole('dialog')).toBeInTheDocument();
});

test('renders the bypassed-cheaper counter when bypassed_cheaper_count is positive', () => {
  const stop = makeStop({
    purchase_reason: 'bypass_cheaper_not_worth_stop',
    bypassed_cheaper_count: 2,
    bypassed_saving_forgone: '12.50',
  });
  render(<JustificationPopup stop={stop} number={1} open onClose={() => {}} />);
  expect(screen.getByText(/passed up 2 cheaper stations/i)).toBeInTheDocument();
  expect(screen.getByText(/12.50/)).toBeInTheDocument();
});

test('omits the bypassed-cheaper counter when bypassed_cheaper_count is zero', () => {
  const stop = makeStop({ purchase_reason: 'reach_finish' });
  render(<JustificationPopup stop={stop} number={1} open onClose={() => {}} />);
  expect(screen.queryByText(/passed up/i)).not.toBeInTheDocument();
});

test('rewords the skipped sentence to describe rejected cheaper candidates, not positional passes', () => {
  const stop = makeStop({
    purchase_reason: 'reach_finish',
    skipped_count: 1,
    skipped_avg_price: '3.75',
  });
  render(<JustificationPopup stop={stop} number={1} open onClose={() => {}} />);
  expect(screen.getByText(/rejected 1 cheaper station in range from here/i)).toBeInTheDocument();
});
