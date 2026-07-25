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

test('states that the solver minimizes total dollars rather than stop count', () => {
  render(<WhyMultipleStopsPopup open onClose={() => {}} />);
  expect(
    screen.getByText(/minimizes total dollars spent on fuel, not the number of stops/i)
  ).toBeInTheDocument();
});

test('explains that stops beyond the range floor are opportunistic cheap-fuel purchases', () => {
  render(<WhyMultipleStopsPopup open onClose={() => {}} />);
  expect(screen.getByText(/fuel was cheap enough there to lower the trip total/i)).toBeInTheDocument();
});

test('clicking the Close control invokes onClose exactly once', () => {
  const onClose = vi.fn();
  render(<WhyMultipleStopsPopup open onClose={onClose} />);
  fireEvent.click(screen.getByRole('button', { name: 'Close' }));
  expect(onClose).toHaveBeenCalledTimes(1);
});
