import { render, screen, cleanup } from '@testing-library/react';
import { afterEach, expect, test } from 'vitest';

import JustificationPopup from './JustificationPopup';
import type { FuelStop, PurchaseReason, Rationale, RouteResponse } from '../../types/routeContract';
// The SAME committed file `routing/tests/test_price_provenance.py` asserts
// against a freshly serialized response (D-19's hop 6/7 -> hop 8 join) --
// a `FuelStopSerializer` key rename now breaks a test on both sides of the
// language boundary, not just one. A plain ESM JSON import: Vite/Vitest
// support it directly and `resolveJsonModule` is already enabled, so no
// config change and no new dependency. Cast once, here, rather than per
// test site: JSON imports infer literal-widened/plain-object types (e.g.
// `station_id` infers as `number`, not this contract's `string | null`),
// so a single explicit cast to the real wire shape keeps every call site
// below fully typed as `FuelStop`.
import routeResponseFixture from '../../test/fixtures/route-response.json';

const fixture = routeResponseFixture as unknown as RouteResponse;

// This file's vite config runs without vitest's `globals` option, so
// testing-library's auto-cleanup detection never fires -- each render
// must be torn down explicitly between tests.
afterEach(cleanup);

function makeStop(
  rationale: Partial<Rationale> & { purchase_reason: PurchaseReason | null },
  overrides: Partial<FuelStop> = {}
): FuelStop {
  return {
    name: 'Test Stop',
    station_id: '1',
    location: { latitude: '41.8781', longitude: '-87.6298' },
    distance_from_start_mi: '200',
    price_per_gallon: '3.50',
    gallons: '40.00',
    cost: '140.00',
    price_source: 'opis_indexed',
    rationale: {
      reason_target_station_id: null,
      reason_target_name: null,
      skipped_count: 0,
      skipped_avg_price: null,
      corridor_avg_price: null,
      price_percentile: null,
      bypassed_cheaper_count: 0,
      bypassed_saving_forgone: null,
      bypassed_estimate_count: 0,
      bypassed_estimate_saving_forgone: null,
      ...rationale,
    },
    ...overrides,
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

test('renders the estimate-bypass sentence with the forgone saving, full text asserted', () => {
  const stop = makeStop({
    purchase_reason: 'bypass_cheaper_not_worth_stop',
    bypassed_estimate_count: 1,
    bypassed_estimate_saving_forgone: '12.50',
  });
  render(<JustificationPopup stop={stop} number={1} open onClose={() => {}} />);
  const expected =
    'Passed over 1 cheaper station whose price is a regional estimate rather than a recorded one, giving up $12.50 in fuel savings.';
  const line = screen.getByText((_, element) => element?.textContent === expected);
  expect(line.textContent).toBe(expected);
});

// Corridor el_paso_tx-portland_me, tag share 0.50, arm
// penalty_aware_heuristic, stop 'QUIKTRIP #667' (opis_id=71647): the real
// corridor-replay witness ROADMAP Phase 21 criterion 5 names, as opposed
// to the hand-built witness the test above pins. Backend counterpart:
// `RealCorridorEstimateBypassWitnessTests` in
// `routing/tests/test_price_provenance.py`, which reproduces this exact
// cell from committed inputs alone and asserts these same two numbers.
test('renders the estimate-bypass sentence for the el_paso_tx-portland_me corridor-replay witness (QUIKTRIP #667), full text asserted', () => {
  const stop = makeStop({
    purchase_reason: 'bypass_cheaper_not_worth_stop',
    bypassed_estimate_count: 1,
    bypassed_estimate_saving_forgone: '1.19',
  });
  render(<JustificationPopup stop={stop} number={1} open onClose={() => {}} />);
  const expected =
    'Passed over 1 cheaper station whose price is a regional estimate rather than a recorded one, giving up $1.19 in fuel savings.';
  const line = screen.getByText((_, element) => element?.textContent === expected);
  expect(line.textContent).toBe(expected);
});

test('renders the estimate-bypass sentence as a complete sentence with no dangling clause when the saving is null', () => {
  const stop = makeStop({
    purchase_reason: 'bypass_cheaper_not_worth_stop',
    bypassed_estimate_count: 2,
    bypassed_estimate_saving_forgone: null,
  });
  render(<JustificationPopup stop={stop} number={1} open onClose={() => {}} />);
  const expected = 'Passed over 2 cheaper stations whose price is a regional estimate rather than a recorded one.';
  const line = screen.getByText((_, element) => element?.textContent === expected);
  expect(line.textContent).toBe(expected);
  expect(line.textContent).not.toMatch(/\$/);
});

test('omits the estimate-bypass sentence when bypassed_estimate_count is zero', () => {
  const stop = makeStop({ purchase_reason: 'reach_finish' });
  render(<JustificationPopup stop={stop} number={1} open onClose={() => {}} />);
  expect(screen.queryByText(/regional estimate rather than a recorded one/i)).not.toBeInTheDocument();
  expect(screen.getByText(/bought just enough fuel here to reach the finish/i)).toBeInTheDocument();
});

test('omits the estimate-bypass sentence and does not throw when both keys are absent (legacy payload)', () => {
  const fullStop = makeStop({ purchase_reason: 'reach_finish' });
  const { bypassed_estimate_count: _unused1, bypassed_estimate_saving_forgone: _unused2, ...legacyRationale } =
    fullStop.rationale;
  const stop: FuelStop = {
    ...fullStop,
    rationale: legacyRationale as unknown as Rationale,
  };
  expect(() =>
    render(<JustificationPopup stop={stop} number={1} open onClose={() => {}} />)
  ).not.toThrow();
  expect(screen.getByRole('dialog')).toBeInTheDocument();
  expect(screen.queryByText(/regional estimate rather than a recorded one/i)).not.toBeInTheDocument();
});

test('the committed fixture renders the estimate-bypass sentence on the recorded-price stop and not on the estimate-priced stop', () => {
  const recordedStop = fixture.fuel_stops.find((s) => s.price_source === 'opis_indexed');
  if (!recordedStop) throw new Error('fixture has no opis_indexed fuel stop');
  const estimateStop = fixture.fuel_stops.find((s) => s.price_source === 'eia_regional_estimate');
  if (!estimateStop) throw new Error('fixture has no eia_regional_estimate fuel stop');

  render(<JustificationPopup stop={recordedStop} number={1} open onClose={() => {}} />);
  const count = recordedStop.rationale.bypassed_estimate_count;
  const expected =
    `Passed over ${count} cheaper station${count === 1 ? '' : 's'} whose price is a regional estimate rather ` +
    `than a recorded one, giving up $${recordedStop.rationale.bypassed_estimate_saving_forgone} in fuel savings.`;
  const line = screen.getByText((_, element) => element?.textContent === expected);
  expect(line.textContent).toBe(expected);
  cleanup();

  render(<JustificationPopup stop={estimateStop} number={1} open onClose={() => {}} />);
  expect(screen.queryByText(/regional estimate rather than a recorded one/i)).not.toBeInTheDocument();
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

test('a recorded-price stop renders the per-gallon line with the recorded-price qualifier', () => {
  // Selected by provenance value, not array index, so a reordering of the
  // fixture does not silently repoint this test at the wrong stop.
  const stop = fixture.fuel_stops.find((s) => s.price_source === 'opis_indexed');
  if (!stop) throw new Error('fixture has no opis_indexed fuel stop');
  render(<JustificationPopup stop={stop} number={1} open onClose={() => {}} />);
  const expected = `$${stop.price_per_gallon}/gal · recorded price`;
  const priceLine = screen.getByText((_, element) => element?.textContent === expected);
  expect(priceLine.textContent).toBe(expected);
});

test('an estimate-sourced stop renders the per-gallon line with the regional-estimate qualifier', () => {
  const stop = fixture.fuel_stops.find((s) => s.price_source === 'eia_regional_estimate');
  if (!stop) throw new Error('fixture has no eia_regional_estimate fuel stop');
  render(<JustificationPopup stop={stop} number={1} open onClose={() => {}} />);
  const expected = `$${stop.price_per_gallon}/gal · regional estimate`;
  const priceLine = screen.getByText((_, element) => element?.textContent === expected);
  expect(priceLine.textContent).toBe(expected);
});

test('a null price_source renders the per-gallon line with no qualifier at all', () => {
  const stop = makeStop(
    { purchase_reason: 'reach_finish' },
    { price_per_gallon: '3.89', price_source: null }
  );
  render(<JustificationPopup stop={stop} number={1} open onClose={() => {}} />);
  const priceLine = screen.getByText((_, element) => element?.textContent === '$3.89/gal');
  expect(priceLine.textContent).toBe('$3.89/gal');
  expect(screen.getByText(/bought just enough fuel here to reach the finish/i)).toBeInTheDocument();
});

test('an unrecognised price_source value renders no qualifier and does not throw', () => {
  const stop = makeStop(
    { purchase_reason: 'reach_finish' },
    { price_per_gallon: '3.89', price_source: 'some_future_source' }
  );
  expect(() =>
    render(<JustificationPopup stop={stop} number={1} open onClose={() => {}} />)
  ).not.toThrow();
  const priceLine = screen.getByText((_, element) => element?.textContent === '$3.89/gal');
  expect(priceLine.textContent).toBe('$3.89/gal');
  expect(screen.getByText(/bought just enough fuel here to reach the finish/i)).toBeInTheDocument();
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
