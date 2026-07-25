import { expect, test, vi } from 'vitest';

import { prewarmServer } from './readyClient';

test('a 200 response resolves without throwing and calls fetch exactly once with /api/ready', async () => {
  const originalFetch = globalThis.fetch;
  const fetchSpy = vi.fn(async () => ({ ok: true, json: async () => ({ status: 'ready' }) }));
  globalThis.fetch = fetchSpy as unknown as typeof fetch;
  try {
    await expect(prewarmServer()).resolves.toBeUndefined();
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(fetchSpy).toHaveBeenCalledWith('/api/ready');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('a 503 response resolves without throwing', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => ({
    ok: false,
    json: async () => ({ status: 'not_ready' }),
  })) as unknown as typeof fetch;
  try {
    await expect(prewarmServer()).resolves.toBeUndefined();
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('a rejected fetch promise resolves without throwing', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => {
    throw new Error('network down');
  }) as unknown as typeof fetch;
  try {
    await expect(prewarmServer()).resolves.toBeUndefined();
  } finally {
    globalThis.fetch = originalFetch;
  }
});
