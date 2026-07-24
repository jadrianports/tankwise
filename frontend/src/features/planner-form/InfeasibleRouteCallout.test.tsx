import { render, screen, cleanup } from '@testing-library/react';
import { afterEach, expect, test } from 'vitest';

import InfeasibleRouteCallout, { type OrderedStop } from './InfeasibleRouteCallout';
import type { RoutePlanError } from '../../hooks/useRoutePlan';

afterEach(cleanup);

const ORDERED_STOPS: OrderedStop[] = [
  { letter: 'A', label: 'Los Angeles' },
  { letter: 'B', label: 'Denver' },
  { letter: 'C', label: 'Chicago' },
];

test('an infeasible_route error with a leg_index names the failing leg by its bounding letters and labels, with a fix suggestion', () => {
  const error: RoutePlanError = {
    code: 'infeasible_route',
    message: 'No feasible fuel plan.',
    detail: { leg_index: 1, leg_coords: [] },
  };

  render(<InfeasibleRouteCallout error={error} orderedStops={ORDERED_STOPS} />);

  const alert = screen.getByRole('alert');
  expect(alert).toHaveTextContent('Leg B→C');
  expect(alert).toHaveTextContent('Denver');
  expect(alert).toHaveTextContent('Chicago');
  expect(alert).toHaveTextContent(/add a stop between them/i);
});

test('a leg_index of 0 names the A→B leg (the first segment)', () => {
  const error: RoutePlanError = {
    code: 'infeasible_route',
    message: 'No feasible fuel plan.',
    detail: { leg_index: 0, leg_coords: [] },
  };

  render(<InfeasibleRouteCallout error={error} orderedStops={ORDERED_STOPS} />);

  expect(screen.getByRole('alert')).toHaveTextContent('Leg A→B');
});

test('a legacy infeasible error with no leg_index renders nothing (falls back to the existing message elsewhere, unchanged)', () => {
  const error: RoutePlanError = {
    code: 'infeasible_route',
    message: 'No feasible fuel plan.',
    detail: { leg_index: null, leg_coords: null },
  };

  const { container } = render(<InfeasibleRouteCallout error={error} orderedStops={ORDERED_STOPS} />);

  expect(container).toBeEmptyDOMElement();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

test('a legacy infeasible error with no detail at all renders nothing', () => {
  const error: RoutePlanError = { code: 'infeasible_route', message: 'No feasible fuel plan.' };

  const { container } = render(<InfeasibleRouteCallout error={error} orderedStops={ORDERED_STOPS} />);

  expect(container).toBeEmptyDOMElement();
});

test('a non-infeasible error code renders nothing', () => {
  const error: RoutePlanError = { code: 'route_not_found', message: 'No drivable route between these points.' };

  const { container } = render(<InfeasibleRouteCallout error={error} orderedStops={ORDERED_STOPS} />);

  expect(container).toBeEmptyDOMElement();
});

test('a null error renders nothing', () => {
  const { container } = render(<InfeasibleRouteCallout error={null} orderedStops={ORDERED_STOPS} />);

  expect(container).toBeEmptyDOMElement();
});
