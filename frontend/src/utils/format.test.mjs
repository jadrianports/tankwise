import { test } from 'node:test';
import assert from 'node:assert/strict';

import { formatGallons, formatMiles } from './format.js';

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
