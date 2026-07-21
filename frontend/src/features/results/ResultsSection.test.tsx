import { render, screen } from '@testing-library/react';
import { expect, test } from 'vitest';

import ResultsSection from './ResultsSection';
import { RoutePlanContext } from '../../context/RoutePlanContext';
import type { RoutePlanContextValue } from '../../context/RoutePlanContext';
import { mapErrorToMessage } from '../../api/routeClient';

const BASE_CONTEXT: RoutePlanContextValue = {
  status: 'idle',
  data: null,
  error: null,
  solve: async () => {},
  retry: () => {},
  focusStop: () => {},
  resolveVehicle: () => {},
};

test('renders the mapErrorToMessage copy inside role="alert" when a plan fails', () => {
  const message = mapErrorToMessage({ code: 'route_not_found', message: 'No route found.', detail: {} });

  render(
    <RoutePlanContext.Provider
      value={{ ...BASE_CONTEXT, status: 'error', error: { code: 'route_not_found', message } }}
    >
      <ResultsSection />
    </RoutePlanContext.Provider>
  );

  expect(screen.getByRole('alert')).toHaveTextContent('No drivable route between these points.');
});

test('shows a Retry button only for an upstream_error, not other error codes', () => {
  const upstreamMessage = mapErrorToMessage({ code: 'upstream_error', message: '', detail: {} });
  const { rerender } = render(
    <RoutePlanContext.Provider
      value={{ ...BASE_CONTEXT, status: 'error', error: { code: 'upstream_error', message: upstreamMessage } }}
    >
      <ResultsSection />
    </RoutePlanContext.Provider>
  );
  expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();

  const notFoundMessage = mapErrorToMessage({ code: 'route_not_found', message: '', detail: {} });
  rerender(
    <RoutePlanContext.Provider
      value={{ ...BASE_CONTEXT, status: 'error', error: { code: 'route_not_found', message: notFoundMessage } }}
    >
      <ResultsSection />
    </RoutePlanContext.Provider>
  );
  expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
});
