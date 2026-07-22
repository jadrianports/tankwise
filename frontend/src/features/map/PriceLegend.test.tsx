import { render, screen, cleanup } from '@testing-library/react';
import { afterEach, expect, test } from 'vitest';

import PriceLegend from './PriceLegend';
import type { CandidateStation } from '../../types/routeContract';
import { computeQuantileBins } from '../../utils/quantile';

// This file's vite config runs without vitest's `globals` option, so
// testing-library's auto-cleanup detection never fires -- each render
// must be torn down explicitly between tests.
afterEach(cleanup);

const LEGEND_NAME = 'Candidate station price legend';

test('renders nothing for an empty candidate array', () => {
  render(<PriceLegend candidates={[]} />);
  expect(screen.queryByRole('group', { name: LEGEND_NAME })).not.toBeInTheDocument();
});

test('renders nothing when no candidate carries a usable price', () => {
  const candidates = [
    { station_id: 'a', price_per_gallon: 'not-a-number' },
    { station_id: 'b', price_per_gallon: undefined },
  ] as unknown as CandidateStation[];

  render(<PriceLegend candidates={candidates} />);
  expect(screen.queryByRole('group', { name: LEGEND_NAME })).not.toBeInTheDocument();
});

test('renders the legend group with its accessible name for a realistic candidate array', () => {
  const candidates = [
    { station_id: 'a', price_per_gallon: '3.10' },
    { station_id: 'b', price_per_gallon: '3.40' },
    { station_id: 'c', price_per_gallon: '3.75' },
    { station_id: 'd', price_per_gallon: '4.05' },
    { station_id: 'e', price_per_gallon: '4.50' },
  ] as unknown as CandidateStation[];

  render(<PriceLegend candidates={candidates} />);
  expect(screen.getByRole('group', { name: LEGEND_NAME })).toBeInTheDocument();
});

test('renders one swatch per threshold plus the top bin', () => {
  const prices = ['3.10', '3.40', '3.75', '4.05', '4.50', '4.90'];
  const candidates = prices.map((price, i) => ({
    station_id: `s${i}`,
    price_per_gallon: price,
  })) as unknown as CandidateStation[];

  const expectedThresholdCount = computeQuantileBins(
    prices.map(Number),
    5
  ).length;

  render(<PriceLegend candidates={candidates} />);
  const group = screen.getByRole('group', { name: LEGEND_NAME });
  const swatches = group.querySelectorAll('[aria-hidden]');
  expect(swatches).toHaveLength(expectedThresholdCount + 1);
});
