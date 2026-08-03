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

// `price_percentile` crosses the wire as a percentage number, not a 0-to-1
// fraction -- `_percent_repr` in routing/serializers.py renders `0.25 -> 25.0`,
// pinned by test_serializers.py. Scaling it again here produced a live
// "beats 2500%" on the Dallas->Seattle demo. The field also counts candidates
// priced strictly BELOW this stop, so it must be inverted to support "beats".
test('treats price_percentile as an already-scaled percentage, not a fraction', () => {
  const stop = makeStop({ purchase_reason: 'reach_finish', price_percentile: 25 });
  render(<JustificationPopup stop={stop} number={1} open onClose={() => {}} />);
  expect(screen.getByText(/beats 75% of the corridor's candidate stations/i)).toBeInTheDocument();
});

test('reports the cheapest station in range as beating every candidate, not none', () => {
  const stop = makeStop({ purchase_reason: 'top_up_at_cheapest', price_percentile: 0 });
  render(<JustificationPopup stop={stop} number={1} open onClose={() => {}} />);
  expect(screen.getByText(/beats 100% of the corridor's candidate stations/i)).toBeInTheDocument();
});

test('reports the most expensive candidate as beating none of them', () => {
  const stop = makeStop({ purchase_reason: 'reach_finish', price_percentile: 100 });
  render(<JustificationPopup stop={stop} number={1} open onClose={() => {}} />);
  expect(screen.getByText(/beats 0% of the corridor's candidate stations/i)).toBeInTheDocument();
});

test('never renders a percentile outside 0-100 for any in-contract API value', () => {
  for (const value of [0, 12.5, 25, 50, 99.9, 100]) {
    const stop = makeStop({ purchase_reason: 'reach_finish', price_percentile: value });
    render(<JustificationPopup stop={stop} number={1} open onClose={() => {}} />);
    const rendered = screen.getByText(/beats \d+% of the corridor/i).textContent ?? '';
    const pct = Number(rendered.match(/beats (\d+)%/)?.[1]);
    expect(pct).toBeGreaterThanOrEqual(0);
    expect(pct).toBeLessThanOrEqual(100);
    cleanup();
  }
});
