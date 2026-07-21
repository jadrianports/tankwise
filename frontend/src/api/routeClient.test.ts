import { expect, test } from 'vitest';

import { mapErrorToMessage, planRoute } from './routeClient';

test('invalid_input with populated detail flattens the specific field message', () => {
  const message = mapErrorToMessage({
    code: 'invalid_input',
    message: 'Invalid request.',
    detail: { start: ['Coordinate (50.4452, -104.6189) is outside the continental US.'] },
  });
  expect(message).toMatch(/outside the continental US/);
  expect(message).not.toBe('Invalid request.');
});

test('invalid_input with empty detail falls back to the specific message', () => {
  const message = mapErrorToMessage({
    code: 'invalid_input',
    message: 'Address must be at most 256 characters, got 300.',
    detail: {},
  });
  expect(message).toBe('Address must be at most 256 characters, got 300.');
});

test('infeasible_route builds the gap-detail sentence from detail', () => {
  const message = mapErrorToMessage({
    code: 'infeasible_route',
    message: 'No feasible fuel plan.',
    detail: {
      from_station: 'Pilot Travel Center',
      to_station: "Love's #123",
      gap_mi: '512.3',
      max_range_mi: '500',
    },
  });
  expect(message).toBe(
    "No fuel stop reachable within 500 mi between Pilot Travel Center and Love's #123 (gap: 512.3 mi)."
  );
});

test('route_not_found returns the fixed copy', () => {
  const message = mapErrorToMessage({
    code: 'route_not_found',
    message: 'No route found.',
    detail: {},
  });
  expect(message).toBe('No drivable route between these points.');
});

test('upstream_error returns the fixed copy', () => {
  const message = mapErrorToMessage({
    code: 'upstream_error',
    message: 'Upstream routing provider failed.',
    detail: {},
  });
  expect(message).toBe('Map service unavailable. Please retry.');
});

test('unknown/missing code falls back to the generic message', () => {
  const message = mapErrorToMessage({ code: 'something_unexpected', message: 'huh', detail: {} });
  expect(message).toBe('Something went wrong. Please try again.');
  expect(mapErrorToMessage(null)).toBe('Something went wrong. Please try again.');
});

test('rate_limited frames the 429 as catching-up, with the countdown seconds', () => {
  const message = mapErrorToMessage({
    code: 'rate_limited',
    message: 'Too many requests.',
    detail: { retry_after_s: 5 },
  });
  expect(message).toMatch(/Catching up/);
  expect(message).toMatch(/5s/);
});

test('config_error returns the map-pane copy without touching the rest of the app', () => {
  const message = mapErrorToMessage({ code: 'config_error', message: 'n/a', detail: {} });
  expect(message).toMatch(/Map unavailable/);
  expect(message).toMatch(/route planner below still works/);
});

test('planRoute POSTs to the relative /api/route path and resolves success', async () => {
  const originalFetch = globalThis.fetch;
  let capturedUrl: RequestInfo | URL | undefined;
  let capturedOptions: RequestInit | undefined;
  globalThis.fetch = (async (url: RequestInfo | URL, options?: RequestInit) => {
    capturedUrl = url;
    capturedOptions = options;
    return { ok: true, json: async () => ({ total_cost: '12.34' }) };
  }) as unknown as typeof fetch;
  try {
    const result = await planRoute('39.7392,-104.9903', '39.0997,-94.5786');
    expect(capturedUrl).toBe('/api/route');
    expect(capturedOptions?.method).toBe('POST');
    expect(JSON.parse(capturedOptions?.body as string).start).toBe('39.7392,-104.9903');
    expect(result.ok).toBe(true);
    expect(result.ok && result.data).toEqual({ total_cost: '12.34' });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('planRoute maps a non-ok response through the error envelope', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => ({
    ok: false,
    json: async () => ({ error: { code: 'route_not_found', message: 'No route found.', detail: {} } }),
  })) as unknown as typeof fetch;
  try {
    const result = await planRoute('33.3879,-118.4163', '34.0522,-118.2437');
    expect(result.ok).toBe(false);
    expect(!result.ok && result.code).toBe('route_not_found');
    expect(!result.ok && result.message).toBe('No drivable route between these points.');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('planRoute falls back to the generic network message when fetch rejects', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => {
    throw new Error('network down');
  }) as unknown as typeof fetch;
  try {
    const result = await planRoute('39.7392,-104.9903', '39.0997,-94.5786');
    expect(result.ok).toBe(false);
    expect(!result.ok && result.code).toBe('network_error');
    expect(!result.ok && result.message).toBe('Something went wrong. Please try again.');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('planRoute passes the AbortSignal straight through to fetch', async () => {
  const originalFetch = globalThis.fetch;
  let capturedOptions: RequestInit | undefined;
  globalThis.fetch = (async (_url: RequestInfo | URL, options?: RequestInit) => {
    capturedOptions = options;
    return { ok: true, json: async () => ({}) };
  }) as unknown as typeof fetch;
  const controller = new AbortController();
  try {
    await planRoute('39.7392,-104.9903', '39.0997,-94.5786', undefined, controller.signal);
    expect(capturedOptions?.signal).toBe(controller.signal);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('planRoute includes the nested vehicle object in the request body when provided', async () => {
  const originalFetch = globalThis.fetch;
  let capturedOptions: RequestInit | undefined;
  globalThis.fetch = (async (_url: RequestInfo | URL, options?: RequestInit) => {
    capturedOptions = options;
    return { ok: true, json: async () => ({}) };
  }) as unknown as typeof fetch;
  try {
    await planRoute('39.7392,-104.9903', '39.0997,-94.5786', { mpg: 6.5, tank_range_mi: 1050, starting_fuel: 1 });
    expect(JSON.parse(capturedOptions?.body as string).vehicle).toEqual({
      mpg: 6.5,
      tank_range_mi: 1050,
      starting_fuel: 1,
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('planRoute omits vehicle from the request body when not provided', async () => {
  const originalFetch = globalThis.fetch;
  let capturedOptions: RequestInit | undefined;
  globalThis.fetch = (async (_url: RequestInfo | URL, options?: RequestInit) => {
    capturedOptions = options;
    return { ok: true, json: async () => ({}) };
  }) as unknown as typeof fetch;
  try {
    await planRoute('39.7392,-104.9903', '39.0997,-94.5786');
    expect('vehicle' in JSON.parse(capturedOptions?.body as string)).toBe(false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('planRoute maps a rate_limited response and surfaces retryAfterS as a number', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => ({
    ok: false,
    json: async () => ({
      error: { code: 'rate_limited', message: 'Too many requests.', detail: { retry_after_s: 7 } },
    }),
  })) as unknown as typeof fetch;
  try {
    const result = await planRoute('39.7392,-104.9903', '39.0997,-94.5786');
    expect(result.ok).toBe(false);
    expect(!result.ok && result.code).toBe('rate_limited');
    expect(!result.ok && result.retryAfterS).toBe(7);
    expect(!result.ok && result.message).toMatch(/Catching up/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('planRoute rethrows AbortError distinctly, never as a network_error result', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => {
    throw new DOMException('The operation was aborted.', 'AbortError');
  }) as unknown as typeof fetch;
  try {
    await expect(planRoute('39.7392,-104.9903', '39.0997,-94.5786')).rejects.toSatisfy(
      (err: unknown) => err instanceof DOMException && err.name === 'AbortError'
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});
