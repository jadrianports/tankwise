import { expect, test } from 'vitest';

import { formatGallons, formatMiles, formatDuration, formatPercent, formatCurrency } from './format';

test('formatGallons rounds a full-precision Decimal string to 2 places + unit', () => {
  expect(formatGallons('10.36625407619502107691084069')).toBe('10.37 gal');
});

test('formatMiles rounds a full-precision Decimal string to a whole number + unit', () => {
  expect(formatMiles('603.6625407619502107691084069')).toBe('604 mi');
});

test('formatMiles adds a locale thousands separator', () => {
  expect(formatMiles('1234.56')).toBe('1,235 mi');
});

test('formatters degrade gracefully on non-numeric input', () => {
  expect(formatGallons('n/a')).toBe('n/a gal');
  expect(formatMiles('n/a')).toBe('n/a mi');
});

test('formatDuration renders hours and minutes for a long trip', () => {
  expect(formatDuration(55200)).toBe('15h 20m');
});

test('formatDuration renders minutes only under an hour', () => {
  expect(formatDuration(300)).toBe('5m');
});

test('formatPercent renders one decimal place with a percent sign', () => {
  expect(formatPercent(12.5)).toBe('12.5%');
});

test('formatCurrency adds a dollar sign and thousands separator', () => {
  expect(formatCurrency('1234.5')).toBe('$1,234.50');
});
