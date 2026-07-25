import { afterEach, expect, test, vi } from 'vitest';

vi.mock('./routeClient', () => ({
  markServerNotAnswering: vi.fn(),
}));

import { prewarmServer } from './readyClient';
import { markServerNotAnswering } from './routeClient';

afterEach(() => {
  vi.clearAllMocks();
});

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

test('a 200 response does not mark the shared not-answering signal', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => ({ ok: true, json: async () => ({ status: 'ready' }) })) as unknown as typeof fetch;
  try {
    await prewarmServer();
    expect(markServerNotAnswering).not.toHaveBeenCalled();
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

test('a non-OK response marks the shared not-answering signal -- a second, independent source alongside planRoute’s own boot-retry loop', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => ({
    ok: false,
    json: async () => ({ status: 'not_ready' }),
  })) as unknown as typeof fetch;
  try {
    await prewarmServer();
    expect(markServerNotAnswering).toHaveBeenCalledTimes(1);
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

test('a rejected fetch promise marks the shared not-answering signal', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => {
    throw new Error('network down');
  }) as unknown as typeof fetch;
  try {
    await prewarmServer();
    expect(markServerNotAnswering).toHaveBeenCalledTimes(1);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
