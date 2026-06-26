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

  it('publicGauntletReport hits the redacted public endpoint', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({ gauntlet_id: 'g1' }));
    const client = new PadrinoClient({ baseUrl: 'http://api', fetchImpl });
    await client.publicGauntletReport('g1');
    const parsed = new URL(fetchImpl.mock.calls[0][0]);
    expect(parsed.pathname).toBe('/public/gauntlets/g1/report');
  });

  it('reads public ruleset metadata for selectors', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      jsonResponse({
        items: [
          {
            ruleset_id: 'roleblock10_v1',
            label: 'Roleblock 10 canonical team',
            player_count: 10,
            rating_context_kind: 'CANONICAL_TEAM',
            is_canonical: true
          }
        ]
      })
    );
    const client = new PadrinoClient({ baseUrl: 'http://api', fetchImpl });
    const response = await client.publicRulesets();
    expect(response.items[0]?.ruleset_id).toBe('roleblock10_v1');
    const parsed = new URL(fetchImpl.mock.calls[0][0]);
    expect(parsed.pathname).toBe('/public/rulesets');
  });

  it('getGauntletReport hits the admin-scoped endpoint', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({ gauntlet_id: 'g1' }));
    const client = new PadrinoClient({ baseUrl: 'http://api', fetchImpl });
    await client.getGauntletReport('g1');
    const parsed = new URL(fetchImpl.mock.calls[0][0]);
    expect(parsed.pathname).toBe('/gauntlets/g1/report');
  });
});

describe('PadrinoClient human-session play channels', () => {
  it('sends cookie credentials only in human-session mode', async () => {
    const fetchImpl = vi.fn().mockImplementation(() => Promise.resolve(jsonResponse({})));
    const spectator = new PadrinoClient({ baseUrl: 'http://api', fetchImpl });
    await spectator.publicLiveIndex();
    expect((fetchImpl.mock.calls[0][1] as RequestInit).credentials).toBeUndefined();

    const human = new PadrinoClient({ baseUrl: 'http://api', humanSession: true, fetchImpl });
    await human.getHumanMe();
    expect((fetchImpl.mock.calls[1][1] as RequestInit).credentials).toBe('include');
    expect(human.hasHumanSession()).toBe(true);
  });

  it('setHumanSession toggles the credential mode', () => {
    const client = new PadrinoClient({ baseUrl: 'http://api' });
    expect(client.hasHumanSession()).toBe(false);
    client.setHumanSession(true);
    expect(client.hasHumanSession()).toBe(true);
    client.setHumanSession(false);
    expect(client.hasHumanSession()).toBe(false);
  });

  it('posts a structured action with an idempotency key', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      jsonResponse({
        accepted: true,
        public_player_id: 'p1',
        phase: 'DAY_1',
        action_type: 'VOTE',
        target: 'p2',
        idempotent_replay: false
      })
    );
    const client = new PadrinoClient({ baseUrl: 'http://api', humanSession: true, fetchImpl });
    const result = await client.submitAction('g1', {
      action: { type: 'VOTE', target: 'p2' },
      idempotency_key: 'k1'
    });
    expect(result.action_type).toBe('VOTE');
    const [url, init] = fetchImpl.mock.calls[0];
    expect(new URL(url).pathname).toBe('/human/games/g1/actions');
    const req = init as RequestInit;
    expect(req.method).toBe('POST');
    expect((req.headers as Record<string, string>)['Content-Type']).toBe('application/json');
    expect(JSON.parse(req.body as string)).toEqual({
      action: { type: 'VOTE', target: 'p2' },
      idempotency_key: 'k1'
    });
  });

  it('posts a chat message into the buffered hold', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      jsonResponse({
        accepted: true,
        public_player_id: 'p1',
        phase: 'DAY_1',
        channel: 'PUBLIC',
        status: 'HELD',
        idempotent_replay: false
      })
    );
    const client = new PadrinoClient({ baseUrl: 'http://api', humanSession: true, fetchImpl });
    await client.submitChat('g1', { channel: 'PUBLIC', text: 'hi', idempotency_key: 'k2' });
    const [url, init] = fetchImpl.mock.calls[0];
    expect(new URL(url).pathname).toBe('/human/games/g1/chat');
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      channel: 'PUBLIC',
      text: 'hi',
      idempotency_key: 'k2'
    });
  });

  it('submits and reads a spot-the-AI guess', async () => {
    const fetchImpl = vi.fn().mockImplementation(() =>
      Promise.resolve(
        jsonResponse({
          guesser_public_id: 'p1',
          total: 6,
          correct: 4,
          accuracy: '0.6667',
          idempotent_replay: false
        })
      )
    );
    const client = new PadrinoClient({ baseUrl: 'http://api', humanSession: true, fetchImpl });
    await client.submitTuringGuess('g1', { p2: 'AI', p3: 'HUMAN' });
    expect(new URL(fetchImpl.mock.calls[0][0]).pathname).toBe('/human/games/g1/turing-guess');
    expect(JSON.parse((fetchImpl.mock.calls[0][1] as RequestInit).body as string)).toEqual({
      guess: { p2: 'AI', p3: 'HUMAN' }
    });
    await client.getTuringGuess('g1');
    expect((fetchImpl.mock.calls[1][1] as RequestInit).method ?? 'GET').toBe('GET');
  });

  it('reads the counts-only composition and both reveal surfaces', async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          game_id: 'g1',
          ruleset_id: 'mini7_v1',
          composition: { human_count: 1, ai_count: 6, total: 7 }
        })
      )
      .mockResolvedValueOnce(
        jsonResponse({ game_id: 'g1', ruleset_id: 'mini7_v1', winner: 'TOWN', seats: [] })
      )
      .mockResolvedValueOnce(
        jsonResponse({ game_id: 'g1', ruleset_id: 'mini7_v1', winner: 'TOWN', seats: [] })
      );
    const client = new PadrinoClient({ baseUrl: 'http://api', fetchImpl });
    const composition = await client.publicGameComposition('g1');
    expect(composition.composition).toEqual({ human_count: 1, ai_count: 6, total: 7 });
    const reveal = await client.publicGameReveal('g1');
    expect(reveal.winner).toBe('TOWN');
    const humanReveal = await client.humanGameReveal('g1');
    expect(humanReveal.winner).toBe('TOWN');
    expect(new URL(fetchImpl.mock.calls[0][0]).pathname).toBe('/public/games/g1/composition');
    expect(new URL(fetchImpl.mock.calls[1][0]).pathname).toBe('/public/games/g1/reveal');
    expect(new URL(fetchImpl.mock.calls[2][0]).pathname).toBe('/human/games/g1/reveal');
  });

  it('drives the lobby create / join / ready / launch surface', async () => {
    const fetchImpl = vi.fn().mockImplementation(() => Promise.resolve(jsonResponse({})));
    const client = new PadrinoClient({ baseUrl: 'http://api', humanSession: true, fetchImpl });
    await client.createLobby({
      ruleset_id: 'mini7_v1',
      identity_mode: 'ANONYMOUS',
      ranked: true,
      integrity_acknowledged: true
    });
    await client.joinLobby('tok');
    await client.setLobbyReady('l1', true);
    await client.launchLobby('l1');
    const paths = fetchImpl.mock.calls.map((c) => new URL(c[0]).pathname);
    expect(paths).toEqual(['/lobbies', '/lobbies/join/tok', '/lobbies/l1/ready', '/lobbies/l1/launch']);
    expect(JSON.parse((fetchImpl.mock.calls[0][1] as RequestInit).body as string)).toEqual({
      ruleset_id: 'mini7_v1',
      identity_mode: 'ANONYMOUS',
      ranked: true,
      integrity_acknowledged: true
    });
    expect(JSON.parse((fetchImpl.mock.calls[2][1] as RequestInit).body as string)).toEqual({
      ready: true
    });
  });

  it('mints a guest and gates human stats by ruleset', async () => {
    const fetchImpl = vi.fn().mockImplementation(() => Promise.resolve(jsonResponse({})));
    const client = new PadrinoClient({ baseUrl: 'http://api', humanSession: true, fetchImpl });
    await client.createGuest();
    expect(new URL(fetchImpl.mock.calls[0][0]).pathname).toBe('/human/guest');
    expect((fetchImpl.mock.calls[0][1] as RequestInit).method).toBe('POST');
    await client.getHumanStats('mini7_v1');
    const statsUrl = new URL(fetchImpl.mock.calls[1][0]);
    expect(statsUrl.pathname).toBe('/human/stats');
    expect(statsUrl.searchParams.get('ruleset_id')).toBe('mini7_v1');
  });

  it('starts a solo human match via the cookie-authenticated match endpoint', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({ game_id: 'g-solo' }));
    const client = new PadrinoClient({ baseUrl: 'http://api', humanSession: true, fetchImpl });
    const match = await client.match();
    const init = fetchImpl.mock.calls[0][1] as RequestInit;
    expect(new URL(fetchImpl.mock.calls[0][0]).pathname).toBe('/human/match');
    expect(init.method).toBe('POST');
    expect(init.credentials).toBe('include');
    expect(match).toEqual({ game_id: 'g-solo' });
  });

  it('throws PadrinoApiError with parsed detail on a failed mutation', async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(jsonResponse({ detail: 'consent_required' }, { status: 412 }));
    const client = new PadrinoClient({ baseUrl: 'http://api', humanSession: true, fetchImpl });
    await expect(
      client.submitAction('g1', { action: { type: 'NOOP' }, idempotency_key: 'k' })
    ).rejects.toMatchObject({ status: 412, detail: { detail: 'consent_required' } });
  });

  it('builds resume-aware live-tail and seat observation URLs', () => {
    const client = new PadrinoClient({ baseUrl: 'http://api' });
    const tailFresh = new URL(client.liveTailUrl('g1'));
    expect(tailFresh.pathname).toBe('/public/games/g1/live');
    expect(tailFresh.searchParams.get('tail')).toBe('true');
    expect(tailFresh.searchParams.has('after')).toBe(false);
    const tailResume = new URL(client.liveTailUrl('g1', 42));
    expect(tailResume.searchParams.get('after')).toBe('42');
    const obs = new URL(client.seatObservationUrl('g1'));
    expect(obs.pathname).toBe('/human/games/g1/observation/stream');
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
