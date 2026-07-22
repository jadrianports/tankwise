import { render, screen, cleanup } from '@testing-library/react';
import { afterEach, expect, test } from 'vitest';

import TankGauge from './TankGauge';

// This file's vite config runs without vitest's `globals` option, so
// testing-library's auto-cleanup detection never fires -- each render
// must be torn down explicitly between tests.
afterEach(cleanup);

test('a 0.5 fraction reports a 50 percent accessible value', () => {
  render(<TankGauge fraction={0.5} />);
  expect(screen.getByRole('progressbar', { name: 'Fuel tank level' })).toHaveAttribute(
    'aria-valuenow',
    '50'
  );
});

test('a negative fraction clamps to 0 percent', () => {
  render(<TankGauge fraction={-0.4} />);
  expect(screen.getByRole('progressbar', { name: 'Fuel tank level' })).toHaveAttribute(
    'aria-valuenow',
    '0'
  );
});

test('a fraction above one clamps to 100 percent', () => {
  render(<TankGauge fraction={1.3} />);
  expect(screen.getByRole('progressbar', { name: 'Fuel tank level' })).toHaveAttribute(
    'aria-valuenow',
    '100'
  );
});
