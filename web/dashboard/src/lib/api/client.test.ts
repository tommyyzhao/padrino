import { describe, expect, it, vi } from 'vitest';
import {
  API_KEY_STORAGE_KEY,
  PadrinoApiError,
  PadrinoClient,
  loadApiKeyFromSession,
  saveApiKeyToSession
} from './client';

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
    ...init
  });
}

describe('PadrinoClient', () => {
  it('builds URLs against the configured base url, dropping nulls and trailing slashes', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({ items: [], next_cursor: null }));
    const client = new PadrinoClient({ baseUrl: 'http://api.example/', fetchImpl });
    await client.listGames({ limit: 5, cursor: null, status: 'COMPLETED' });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url] = fetchImpl.mock.calls[0];
    const parsed = new URL(url);
    expect(parsed.origin).toBe('http://api.example');
    expect(parsed.pathname).toBe('/games');
    expect(parsed.searchParams.get('limit')).toBe('5');
    expect(parsed.searchParams.get('status')).toBe('COMPLETED');
    expect(parsed.searchParams.has('cursor')).toBe(false);
  });

  it('attaches a Bearer Authorization header when an api key is set', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({ items: [], next_cursor: null }));
    const client = new PadrinoClient({ baseUrl: 'http://api', apiKey: 'pk_secret', fetchImpl });
    await client.listGames();
    const init = fetchImpl.mock.calls[0][1] as RequestInit;
    expect(init.headers).toMatchObject({ Authorization: 'Bearer pk_secret' });
  });

  it('omits Authorization header when no api key is set', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({ items: [], next_cursor: null }));
    const client = new PadrinoClient({ baseUrl: 'http://api', fetchImpl });
    await client.listGames();
    const init = fetchImpl.mock.calls[0][1] as RequestInit;
    const headers = init.headers as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();
  });

  it('throws PadrinoApiError on non-2xx responses', async () => {
    const fetchImpl = vi
      .fn()
      .mockImplementation(() =>
        Promise.resolve(jsonResponse({ detail: 'invalid_cursor' }, { status: 400 }))
      );
    const client = new PadrinoClient({ baseUrl: 'http://api', fetchImpl });
    await expect(client.listGames()).rejects.toBeInstanceOf(PadrinoApiError);
    try {
      await client.listGames();
    } catch (err) {
      expect((err as PadrinoApiError).status).toBe(400);
      expect((err as PadrinoApiError).detail).toEqual({ detail: 'invalid_cursor' });
    }
  });

  it('calls the public game events endpoint with cursor pagination', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      jsonResponse({ game_id: 'g1', items: [], next_cursor: null, total_estimate: 0 })
    );
    const client = new PadrinoClient({ baseUrl: 'http://api', fetchImpl });
    await client.publicGameEvents('g1', { limit: 50, cursor: 'abc' });
    const parsed = new URL(fetchImpl.mock.calls[0][0]);
    expect(parsed.pathname).toBe('/public/games/g1/events');
    expect(parsed.searchParams.get('limit')).toBe('50');
    expect(parsed.searchParams.get('cursor')).toBe('abc');
  });

  it('hasApiKey reflects setApiKey', () => {
    const client = new PadrinoClient({ baseUrl: 'http://api' });
    expect(client.hasApiKey()).toBe(false);
    client.setApiKey('pk_x');
    expect(client.hasApiKey()).toBe(true);
    client.setApiKey(null);
    expect(client.hasApiKey()).toBe(false);
  });

  it('encodes path-segment ids to avoid traversal', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({}));
    const client = new PadrinoClient({ baseUrl: 'http://api', fetchImpl });
    await client.getGame('with/slash');
    expect(fetchImpl.mock.calls[0][0]).toBe('http://api/games/with%2Fslash');
  });
});

describe('session storage', () => {
  it('round-trips via sessionStorage and removes on null', () => {
    // jsdom provides sessionStorage.
    saveApiKeyToSession('pk_test');
    expect(window.sessionStorage.getItem(API_KEY_STORAGE_KEY)).toBe('pk_test');
    expect(loadApiKeyFromSession()).toBe('pk_test');
    saveApiKeyToSession(null);
    expect(window.sessionStorage.getItem(API_KEY_STORAGE_KEY)).toBeNull();
    expect(loadApiKeyFromSession()).toBeNull();
  });
});
