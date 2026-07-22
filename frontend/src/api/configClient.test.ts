import { expect, test } from 'vitest';

import { fetchConfig } from './configClient';

test('a fetch that rejects resolves to a config_error rather than throwing', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => {
    throw new Error('network down');
  }) as unknown as typeof fetch;
  try {
    const result = await fetchConfig();
    expect(result).toEqual({ ok: false, code: 'config_error' });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('a non-ok response resolves to a config_error', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => ({
    ok: false,
    json: async () => ({ mapbox_public_token: 'pk.placeholder-token' }),
  })) as unknown as typeof fetch;
  try {
    const result = await fetchConfig();
    expect(result).toEqual({ ok: false, code: 'config_error' });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('an ok response whose body fails to parse as JSON resolves to a config_error', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => ({
    ok: true,
    json: async () => {
      throw new Error('unexpected token');
    },
  })) as unknown as typeof fetch;
  try {
    const result = await fetchConfig();
    expect(result).toEqual({ ok: false, code: 'config_error' });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('an ok response with a body missing a usable public token resolves to a config_error', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => ({
    ok: true,
    json: async () => ({ price_as_of: '2025-01-01', price_data_note: 'note' }),
  })) as unknown as typeof fetch;
  try {
    const result = await fetchConfig();
    expect(result).toEqual({ ok: false, code: 'config_error' });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('an ok response with a well-formed body resolves to the parsed config data', async () => {
  const originalFetch = globalThis.fetch;
  const body = {
    mapbox_public_token: 'pk.placeholder-token',
    price_as_of: '2025-01-01',
    price_data_note: 'note',
  };
  globalThis.fetch = (async () => ({
    ok: true,
    json: async () => body,
  })) as unknown as typeof fetch;
  try {
    const result = await fetchConfig();
    expect(result).toEqual({ ok: true, data: body });
  } finally {
    globalThis.fetch = originalFetch;
  }
});
