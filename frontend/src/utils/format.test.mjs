import { test } from 'node:test';
import assert from 'node:assert/strict';

import { formatGallons, formatMiles, formatDuration, formatPercent, formatCurrency } from './format.ts';

test('formatGallons rounds a full-precision Decimal string to 2 places + unit', () => {
  assert.equal(formatGallons('10.36625407619502107691084069'), '10.37 gal');
});

test('formatMiles rounds a full-precision Decimal string to a whole number + unit', () => {
  assert.equal(formatMiles('603.6625407619502107691084069'), '604 mi');
});

test('formatMiles adds a locale thousands separator', () => {
  assert.equal(formatMiles('1234.56'), '1,235 mi');
});

test('formatters degrade gracefully on non-numeric input', () => {
  assert.equal(formatGallons('n/a'), 'n/a gal');
  assert.equal(formatMiles('n/a'), 'n/a mi');
});

test('formatDuration renders hours and minutes for a long trip', () => {
  assert.equal(formatDuration(55200), '15h 20m');
});

test('formatDuration renders minutes only under an hour', () => {
  assert.equal(formatDuration(300), '5m');
});

test('formatPercent renders one decimal place with a percent sign', () => {
  assert.equal(formatPercent(12.5), '12.5%');
});

test('formatCurrency adds a dollar sign and thousands separator', () => {
  assert.equal(formatCurrency('1234.5'), '$1,234.50');
});
