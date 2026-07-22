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
