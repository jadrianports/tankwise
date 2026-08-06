import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { afterEach, expect, test } from 'vitest';

import SummaryCard from './SummaryCard';
import type { RouteResponse } from '../../types/routeContract';

// This file's vite config runs without vitest's `globals` option, so
// testing-library's auto-cleanup detection never fires -- each render
// must be torn down explicitly between tests.
afterEach(cleanup);

// SummaryCard reads no context -- only the fields it actually touches are
// filled in, cast through `unknown` to skip the rest of the contract.
// No price_index_status here on purpose: this fixture models a
// pre-phase/legacy payload, which must still render the original
// "Prices as of ..." disclaimer and never a trend chip.
const BASE_FIXTURE = {
  total_cost: '245.67',
  vehicle: { starting_fuel_mi: '500' },
  savings: { amount: '32.50', percent: 11.7 },
  savings_note: null,
  alternatives_considered: 3,
  price_as_of: '2025-01-01',
  price_data_note: 'Prices reflect the most recent available data.',
} as unknown as RouteResponse;

test('a non-zero total cost renders the currency-formatted total', () => {
  render(<SummaryCard data={BASE_FIXTURE} />);

  expect(screen.getByText('$245.67')).toBeInTheDocument();
});

test('a response carrying a savings object renders the savings figure and its percent', () => {
  render(<SummaryCard data={BASE_FIXTURE} />);

  expect(screen.getByText('Save $32.50 (11.7%)')).toBeInTheDocument();
});

test('a response with no savings object omits the savings block and shows the note instead', () => {
  const data = {
    ...BASE_FIXTURE,
    savings: null,
    savings_note: 'Savings could not be compared for this trip.',
  } as unknown as RouteResponse;

  render(<SummaryCard data={data} />);

  expect(screen.queryByText(/^Save \$/)).not.toBeInTheDocument();
  expect(screen.getByText('Savings could not be compared for this trip.')).toBeInTheDocument();
});

test('changing the hauls-per-week input updates the rendered fleet-annualised figure', () => {
  render(<SummaryCard data={BASE_FIXTURE} />);

  expect(screen.getByText('~$8,450.00/year at')).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText('Hauls per week'), { target: { value: '10' } });

  expect(screen.getByText('~$16,900.00/year at')).toBeInTheDocument();
});

test('the footer renders the alternatives-considered badge alongside the price vintage', () => {
  render(<SummaryCard data={BASE_FIXTURE} />);

  expect(screen.getByText("Compared 3 route options — this one's cheapest.")).toBeInTheDocument();
  expect(
    screen.getByText('Prices as of 2025-01-01. Prices reflect the most recent available data.')
  ).toBeInTheDocument();
});

test('a current status with a positive delta renders a rising trend chip and the indexed-week disclaimer', () => {
  const data = {
    ...BASE_FIXTURE,
    price_index_status: 'current',
    eia_week: '2026-07-20',
    trend_region: 'Midwest',
    trend_delta_cents: 4,
    price_data_note: 'Station prices indexed to the EIA week of Jul 20, 2026.',
  } as unknown as RouteResponse;

  render(<SummaryCard data={data} />);

  expect(screen.getByText('Midwest diesel ▲ 4¢ this week')).toBeInTheDocument();
  expect(
    screen.getByText('Station prices indexed to the EIA week of Jul 20, 2026.')
  ).toBeInTheDocument();
  expect(screen.queryByText(/^Prices as of/)).not.toBeInTheDocument();
});

test('a current status with a negative delta renders a falling trend chip', () => {
  const data = {
    ...BASE_FIXTURE,
    price_index_status: 'current',
    eia_week: '2026-07-20',
    trend_region: 'Gulf Coast',
    trend_delta_cents: -3,
    price_data_note: 'Station prices indexed to the EIA week of Jul 20, 2026.',
  } as unknown as RouteResponse;

  render(<SummaryCard data={data} />);

  expect(screen.getByText('Gulf Coast diesel ▼ 3¢ this week')).toBeInTheDocument();
});

test('a current status with a flat delta renders the steady chip with no arrow', () => {
  const data = {
    ...BASE_FIXTURE,
    price_index_status: 'current',
    eia_week: '2026-07-20',
    trend_region: 'Midwest',
    trend_delta_cents: 0,
    price_data_note: 'Station prices indexed to the EIA week of Jul 20, 2026.',
  } as unknown as RouteResponse;

  render(<SummaryCard data={data} />);

  expect(screen.getByText('diesel steady this week')).toBeInTheDocument();
  expect(screen.queryByText(/▲|▼/)).not.toBeInTheDocument();
});

test('a stale status renders the "(latest available)" disclaimer and no chip', () => {
  const data = {
    ...BASE_FIXTURE,
    price_index_status: 'stale',
    eia_week: '2026-07-06',
    trend_region: 'Midwest',
    trend_delta_cents: 4,
    price_data_note: 'Station prices indexed to the EIA week of Jul 6, 2026 (latest available).',
  } as unknown as RouteResponse;

  render(<SummaryCard data={data} />);

  expect(
    screen.getByText('Station prices indexed to the EIA week of Jul 6, 2026 (latest available).')
  ).toBeInTheDocument();
  expect(screen.queryByText(/diesel/)).not.toBeInTheDocument();
});

test('a frozen status renders the legacy disclaimer and no chip', () => {
  const data = {
    ...BASE_FIXTURE,
    price_index_status: 'frozen',
    eia_week: null,
    trend_region: null,
    trend_delta_cents: null,
  } as unknown as RouteResponse;

  render(<SummaryCard data={data} />);

  expect(
    screen.getByText('Prices as of 2025-01-01. Prices reflect the most recent available data.')
  ).toBeInTheDocument();
  expect(screen.queryByText(/diesel/)).not.toBeInTheDocument();
});

test('a response carrying a composition string renders it directly beneath the disclaimer', () => {
  const data = {
    ...BASE_FIXTURE,
    station_data_note: '6,290 stations — all with recorded prices.',
  } as unknown as RouteResponse;

  render(<SummaryCard data={data} />);

  expect(screen.getByText('6,290 stations — all with recorded prices.')).toBeInTheDocument();
});

test('the composition line renders identically across current, stale, and legacy price_index_status', () => {
  const note = '6,290 stations — all with recorded prices.';

  const current = {
    ...BASE_FIXTURE,
    price_index_status: 'current',
    trend_region: 'Midwest',
    trend_delta_cents: 4,
    station_data_note: note,
  } as unknown as RouteResponse;
  const { unmount: unmountCurrent } = render(<SummaryCard data={current} />);
  expect(screen.getByText(note)).toBeInTheDocument();
  unmountCurrent();

  const stale = {
    ...BASE_FIXTURE,
    price_index_status: 'stale',
    station_data_note: note,
  } as unknown as RouteResponse;
  const { unmount: unmountStale } = render(<SummaryCard data={stale} />);
  expect(screen.getByText(note)).toBeInTheDocument();
  unmountStale();

  // Legacy fixture: no price_index_status at all (BASE_FIXTURE's own shape).
  const legacy = {
    ...BASE_FIXTURE,
    station_data_note: note,
  } as unknown as RouteResponse;
  render(<SummaryCard data={legacy} />);
  expect(screen.getByText(note)).toBeInTheDocument();
});

test('an empty-string composition value renders no element at all', () => {
  const data = {
    ...BASE_FIXTURE,
    station_data_note: '',
  } as unknown as RouteResponse;

  render(<SummaryCard data={data} />);

  expect(screen.queryByText(/stations —/)).not.toBeInTheDocument();
});

test('a legacy fixture omitting station_data_note entirely renders no element and does not throw', () => {
  expect(() => render(<SummaryCard data={BASE_FIXTURE} />)).not.toThrow();

  expect(screen.queryByText(/stations —/)).not.toBeInTheDocument();
});

test('the trend chip carries the EIA source-citation tooltip title and is keyboard-reachable', () => {
  const data = {
    ...BASE_FIXTURE,
    price_index_status: 'current',
    eia_week: '2026-07-20',
    trend_region: 'Midwest',
    trend_delta_cents: 4,
    price_data_note: 'Station prices indexed to the EIA week of Jul 20, 2026.',
  } as unknown as RouteResponse;

  render(<SummaryCard data={data} />);

  // MUI's Tooltip clones its child with an aria-label carrying the
  // title text, making the source citation available to assistive tech
  // without requiring the popper to be open. The Chip's own tabIndex=0
  // (asserted below) is what makes it reachable via keyboard Tab order.
  const chip = screen.getByLabelText(
    'Week-over-week change in the U.S. EIA on-highway diesel average for this region.'
  );
  expect(chip).toBeInTheDocument();
  expect(chip).toHaveTextContent('Midwest diesel ▲ 4¢ this week');
  expect(chip).toHaveAttribute('tabindex', '0');
});
