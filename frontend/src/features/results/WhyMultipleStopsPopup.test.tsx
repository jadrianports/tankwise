import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';

import WhyMultipleStopsPopup from './WhyMultipleStopsPopup';

// This file's vite config runs without vitest's `globals` option, so
// testing-library's auto-cleanup detection never fires -- each render
// must be torn down explicitly between tests.
afterEach(cleanup);

test('renders nothing in the document when open is false', () => {
  render(<WhyMultipleStopsPopup open={false} onClose={() => {}} />);
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

test('renders a dialog with an accessible name naming the multiple-stops question when open is true', () => {
  render(<WhyMultipleStopsPopup open onClose={() => {}} />);
  expect(screen.getByRole('dialog', { name: /why multiple fuel stops/i })).toBeInTheDocument();
});

test('states that a stop is only taken when the cheaper fuel beats the cost of stopping', () => {
  render(<WhyMultipleStopsPopup open onClose={() => {}} />);
  expect(
    screen.getByText(/only takes a stop when the cheaper fuel there beats the cost of pulling over/i)
  ).toBeInTheDocument();
});

test('explains the physical tank-range floor and that the remaining stops pay for themselves', () => {
  render(<WhyMultipleStopsPopup open onClose={() => {}} />);
  expect(
    screen.getByText(/some stops are unavoidable no matter how the fuel is bought/i)
  ).toBeInTheDocument();
});

test('states the roughly $35 per-stop charge and its industry provenance', () => {
  render(<WhyMultipleStopsPopup open onClose={() => {}} />);
  expect(screen.getByText(/about \$35 on industry operating-cost figures/i)).toBeInTheDocument();
});

test('discloses that the cost shown is fuel only', () => {
  render(<WhyMultipleStopsPopup open onClose={() => {}} />);
  expect(screen.getByText(/The cost shown is fuel only/i)).toBeInTheDocument();
});

test('clicking the Close control invokes onClose exactly once', () => {
  const onClose = vi.fn();
  render(<WhyMultipleStopsPopup open onClose={onClose} />);
  fireEvent.click(screen.getByRole('button', { name: 'Close' }));
  expect(onClose).toHaveBeenCalledTimes(1);
});
