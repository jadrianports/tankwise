import { renderHook, waitFor, act } from '@testing-library/react';
import { vi, expect, test, beforeEach } from 'vitest';

import { useRoutePlan } from './useRoutePlan';
import { planRoute } from '../api/routeClient';
import type { PlanRouteResult } from '../api/routeClient';
import type { RouteResponse } from '../types/routeContract';

// Mock the module boundary (../api/routeClient's planRoute), not fetch --
// this is the exact seam useRoutePlan calls through, so mocking here
// exercises the hook's own state-machine logic without needing a
// fetch/Response/AbortSignal polyfill stack.
vi.mock('../api/routeClient', () => ({
  planRoute: vi.fn(),
}));

const mockedPlanRoute = vi.mocked(planRoute);

beforeEach(() => {
  mockedPlanRoute.mockReset();
});

const FIRST_ROUTE = { total_cost: '100.00' } as unknown as RouteResponse;
const SECOND_ROUTE = { total_cost: '222.22' } as unknown as RouteResponse;

test('submit transitions idle -> loading -> success with non-null data', async () => {
  mockedPlanRoute.mockResolvedValue({ ok: true, data: FIRST_ROUTE });

  const { result } = renderHook(() => useRoutePlan());
  expect(result.current.status).toBe('idle');

  act(() => {
    void result.current.submit('32.7767,-96.7970', '34.0522,-118.2437');
  });
  expect(result.current.status).toBe('loading');

  await waitFor(() => expect(result.current.status).toBe('success'));
  expect(result.current.data).not.toBeNull();
  expect(result.current.data).toEqual(FIRST_ROUTE);
});

test('a non-rate-limited failure sets status to error and clears data', async () => {
  mockedPlanRoute.mockResolvedValueOnce({ ok: true, data: FIRST_ROUTE });
  const { result } = renderHook(() => useRoutePlan());
  act(() => {
    void result.current.submit('32.7767,-96.7970', '34.0522,-118.2437');
  });
  await waitFor(() => expect(result.current.status).toBe('success'));

  mockedPlanRoute.mockResolvedValueOnce({
    ok: false,
    code: 'route_not_found',
    message: 'No drivable route between these points.',
  });
  act(() => {
    void result.current.submit('32.7767,-96.7970', '34.0522,-118.2437');
  });

  await waitFor(() => expect(result.current.status).toBe('error'));
  expect(result.current.data).toBeNull();
  expect(result.current.error?.code).toBe('route_not_found');
});

test('a rate_limited failure keeps the previous data intact and exposes retryAfterS', async () => {
  mockedPlanRoute.mockResolvedValueOnce({ ok: true, data: FIRST_ROUTE });
  const { result } = renderHook(() => useRoutePlan());
  act(() => {
    void result.current.submit('32.7767,-96.7970', '34.0522,-118.2437');
  });
  await waitFor(() => expect(result.current.status).toBe('success'));

  mockedPlanRoute.mockResolvedValueOnce({
    ok: false,
    code: 'rate_limited',
    message: 'Catching up — retrying in 5s',
    retryAfterS: 5,
  });
  act(() => {
    void result.current.submit('32.7767,-96.7970', '34.0522,-118.2437');
  });

  await waitFor(() => expect(result.current.status).toBe('rate_limited'));
  // `data` must NOT be cleared by a 429 -- the last good plan
  // stays fully visible while the cooldown counts down.
  expect(result.current.data).toEqual(FIRST_ROUTE);
  expect(result.current.error?.code).toBe('rate_limited');
  expect(result.current.error?.retryAfterS).toBe(5);
});

test('the monotonic sequenceRef guard keeps a stale response from overwriting a newer one', async () => {
  let resolveFirst!: (value: PlanRouteResult) => void;
  const firstPromise = new Promise<PlanRouteResult>((resolve) => {
    resolveFirst = resolve;
  });

  // First submit's planRoute call never resolves until we manually resolve
  // it below (after the second submit has already completed) -- this
  // reproduces "the first call resolves AFTER the second" ordering.
  mockedPlanRoute.mockImplementationOnce(() => firstPromise);
  mockedPlanRoute.mockResolvedValueOnce({ ok: true, data: SECOND_ROUTE });

  const { result } = renderHook(() => useRoutePlan());

  act(() => {
    void result.current.submit('first-start', 'first-finish');
  });
  act(() => {
    void result.current.submit('second-start', 'second-finish');
  });

  await waitFor(() => expect(result.current.status).toBe('success'));
  expect(result.current.data).toEqual(SECOND_ROUTE);

  // Now let the stale first call resolve -- it must be a no-op, since its
  // captured sequence number no longer matches sequenceRef.current.
  await act(async () => {
    resolveFirst({ ok: true, data: FIRST_ROUTE });
    await Promise.resolve();
  });

  expect(result.current.status).toBe('success');
  expect(result.current.data).toEqual(SECOND_ROUTE);
});

test('a superseded in-flight call whose planRoute rejects with an AbortError never surfaces as an error', async () => {
  mockedPlanRoute.mockImplementationOnce(() =>
    Promise.reject(new DOMException('The operation was aborted.', 'AbortError'))
  );
  mockedPlanRoute.mockResolvedValueOnce({ ok: true, data: SECOND_ROUTE });

  const { result } = renderHook(() => useRoutePlan());

  act(() => {
    void result.current.submit('first-start', 'first-finish');
  });
  act(() => {
    void result.current.submit('second-start', 'second-finish');
  });

  await waitFor(() => expect(result.current.status).toBe('success'));
  expect(result.current.error).toBeNull();
  expect(result.current.data).toEqual(SECOND_ROUTE);
});
