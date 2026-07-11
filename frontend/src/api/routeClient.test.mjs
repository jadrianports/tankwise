import { test } from 'node:test';
import assert from 'node:assert/strict';

import { mapErrorToMessage, planRoute } from './routeClient.js';

test('invalid_input with populated detail flattens the specific field message', () => {
  const message = mapErrorToMessage({
    code: 'invalid_input',
    message: 'Invalid request.',
    detail: { start: ['Coordinate (50.4452, -104.6189) is outside the continental US.'] },
  });
  assert.match(message, /outside the continental US/);
  assert.notEqual(message, 'Invalid request.');
});

test('invalid_input with empty detail falls back to the specific message', () => {
  const message = mapErrorToMessage({
    code: 'invalid_input',
    message: 'Address must be at most 256 characters, got 300.',
    detail: {},
  });
  assert.equal(message, 'Address must be at most 256 characters, got 300.');
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
  assert.equal(
    message,
    "No fuel stop reachable within 500 mi between Pilot Travel Center and Love's #123 (gap: 512.3 mi)."
  );
});

test('route_not_found returns the fixed State Contract copy', () => {
  const message = mapErrorToMessage({
    code: 'route_not_found',
    message: 'No route found.',
    detail: {},
  });
  assert.equal(message, 'No drivable route between these points.');
});

test('upstream_error returns the fixed State Contract copy', () => {
  const message = mapErrorToMessage({
    code: 'upstream_error',
    message: 'Upstream routing provider failed.',
    detail: {},
  });
  assert.equal(message, 'Map service unavailable — please retry.');
});

test('unknown/missing code falls back to the generic message', () => {
  const message = mapErrorToMessage({ code: 'something_unexpected', message: 'huh', detail: {} });
  assert.equal(message, 'Something went wrong. Please try again.');
  assert.equal(mapErrorToMessage(null), 'Something went wrong. Please try again.');
});

test('planRoute POSTs to the relative /api/route path and resolves success', async () => {
  const originalFetch = global.fetch;
  let capturedUrl;
  let capturedOptions;
  global.fetch = async (url, options) => {
    capturedUrl = url;
    capturedOptions = options;
    return { ok: true, json: async () => ({ total_cost: '12.34' }) };
  };
  try {
    const result = await planRoute('39.7392,-104.9903', '39.0997,-94.5786');
    assert.equal(capturedUrl, '/api/route');
    assert.equal(capturedOptions.method, 'POST');
    assert.equal(JSON.parse(capturedOptions.body).start, '39.7392,-104.9903');
    assert.equal(result.ok, true);
    assert.deepEqual(result.data, { total_cost: '12.34' });
  } finally {
    global.fetch = originalFetch;
  }
});

test('planRoute maps a non-ok response through the error envelope', async () => {
  const originalFetch = global.fetch;
  global.fetch = async () => ({
    ok: false,
    json: async () => ({ error: { code: 'route_not_found', message: 'No route found.', detail: {} } }),
  });
  try {
    const result = await planRoute('33.3879,-118.4163', '34.0522,-118.2437');
    assert.equal(result.ok, false);
    assert.equal(result.code, 'route_not_found');
    assert.equal(result.message, 'No drivable route between these points.');
  } finally {
    global.fetch = originalFetch;
  }
});

test('planRoute falls back to the generic network message when fetch rejects', async () => {
  const originalFetch = global.fetch;
  global.fetch = async () => {
    throw new Error('network down');
  };
  try {
    const result = await planRoute('39.7392,-104.9903', '39.0997,-94.5786');
    assert.equal(result.ok, false);
    assert.equal(result.code, 'network_error');
    assert.equal(result.message, 'Something went wrong. Please try again.');
  } finally {
    global.fetch = originalFetch;
  }
});
