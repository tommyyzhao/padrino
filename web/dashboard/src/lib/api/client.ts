import type {
  ActionInput,
  ActionResult,
  ChatChannel,
  ChatResult,
  ConsentStatus,
  CursorPage,
  EndgameReveal,
  GameDetailResponse,
  GameListEntry,
  GauntletListEntry,
  GauntletReport,
  GuestSummary,
  HumanPlayerStats,
  LaunchResponse,
  LobbyRoster,
  LobbySummary,
  PublicCompositionResponse,
  PublicEventsResponse,
  PublicGameAnalyticsResponse,
  PublicGameResponse,
  PublicLadderResponse,
  PublicLeaderboardResponse,
  PublicLiveIndexResponse,
  PublicModelAnalyticsResponse,
  PublicModelLeaderboardResponse,
  PublicRecentIndexResponse,
  PublicRulesetsResponse,
  SeatGuess,
  TuringGuessResult
} from './types';

export const DEFAULT_BASE_URL = 'http://localhost:8000';

export class PadrinoApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, detail: unknown, message?: string) {
    super(message ?? `padrino api error ${status}`);
    this.name = 'PadrinoApiError';
    this.status = status;
    this.detail = detail;
  }
}

export interface PadrinoClientOptions {
  baseUrl?: string;
  apiKey?: string | null;
  /**
   * Human-session credential mode (Wave 9). When enabled, requests are sent
   * with cookie credentials (`credentials: 'include'`) so the backend's
   * http-only human session cookie authenticates the call. This is the
   * play-client credential alongside the spectator API key: a human session
   * carries ZERO API scope and an API key carries ZERO human identity.
   */
  humanSession?: boolean;
  fetchImpl?: typeof fetch;
}

export class PadrinoClient {
  readonly baseUrl: string;
  private apiKey: string | null;
  private humanSession: boolean;
  private readonly fetchImpl: typeof fetch;

  constructor(options: PadrinoClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? DEFAULT_BASE_URL).replace(/\/+$/, '');
    this.apiKey = options.apiKey ?? null;
    this.humanSession = options.humanSession ?? false;
    this.fetchImpl = options.fetchImpl ?? fetch.bind(globalThis);
  }

  setApiKey(key: string | null): void {
    this.apiKey = key;
  }

  hasApiKey(): boolean {
    return this.apiKey !== null && this.apiKey !== '';
  }

  /**
   * Enable/disable the human-session credential mode. A human session is a
   * cookie-based identity (set http-only by the backend on guest quickplay /
   * OAuth), so the client only needs to opt into sending cookies — it never
   * holds the session token itself.
   */
  setHumanSession(enabled: boolean): void {
    this.humanSession = enabled;
  }

  hasHumanSession(): boolean {
    return this.humanSession;
  }

  private buildUrl(path: string, params?: Record<string, string | number | null | undefined>): string {
    const url = new URL(this.baseUrl + path);
    if (params) {
      for (const [key, value] of Object.entries(params)) {
        if (value === null || value === undefined || value === '') continue;
        url.searchParams.set(key, String(value));
      }
    }
    return url.toString();
  }

  private authHeaders(): Record<string, string> {
    const headers: Record<string, string> = { Accept: 'application/json' };
    if (this.apiKey) {
      headers.Authorization = `Bearer ${this.apiKey}`;
    }
    return headers;
  }

  private async parseError(response: Response): Promise<never> {
    let detail: unknown = null;
    try {
      detail = await response.json();
    } catch {
      // ignore non-JSON error bodies
    }
    throw new PadrinoApiError(response.status, detail);
  }

  private async request<T>(path: string, params?: Record<string, string | number | null | undefined>): Promise<T> {
    const init: RequestInit = { headers: this.authHeaders() };
    if (this.humanSession) {
      init.credentials = 'include';
    }
    const response = await this.fetchImpl(this.buildUrl(path, params), init);
    if (!response.ok) {
      await this.parseError(response);
    }
    return (await response.json()) as T;
  }

  /**
   * Authenticated mutation (POST/PATCH) carrying a JSON body. Used by the human
   * play channels (action / chat / guess / consent) and lobby mutations: these
   * authenticate via the cookie-based human session, so they always send cookie
   * credentials. The body is JSON-serialised; a non-2xx response throws a
   * {@link PadrinoApiError} carrying the parsed detail.
   */
  private async mutate<T>(
    method: 'POST' | 'PATCH',
    path: string,
    body?: unknown
  ): Promise<T> {
    const headers = this.authHeaders();
    const init: RequestInit = { method, headers };
    if (this.humanSession) {
      init.credentials = 'include';
    }
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    const response = await this.fetchImpl(this.buildUrl(path), init);
    if (!response.ok) {
      await this.parseError(response);
    }
    return (await response.json()) as T;
  }

  // ---- public (unauthenticated when padrino_public_leaderboard_anonymous=True)

  publicLeaderboard(params: {
    ruleset_id?: string | null;
    gauntlet_id?: string | null;
    limit?: number;
    cursor?: string | null;
  }): Promise<PublicLeaderboardResponse> {
    return this.request('/public/leaderboard', params);
  }

  publicModelLeaderboard(params: {
    ruleset_id: string;
    league_id: string;
    limit?: number;
    cursor?: string | null;
  }): Promise<PublicModelLeaderboardResponse> {
    return this.request('/public/models/leaderboard', params);
  }

  publicGame(gameId: string): Promise<PublicGameResponse> {
    return this.request(`/public/games/${encodeURIComponent(gameId)}`);
  }

  publicGameEvents(
    gameId: string,
    params: { limit?: number; cursor?: string | null } = {}
  ): Promise<PublicEventsResponse> {
    return this.request(`/public/games/${encodeURIComponent(gameId)}/events`, params);
  }

  publicLiveIndex(): Promise<PublicLiveIndexResponse> {
    return this.request('/public/live');
  }

  publicRecentIndex(params: { limit?: number; cursor?: string | null } = {}): Promise<PublicRecentIndexResponse> {
    return this.request('/public/recent', params);
  }

  publicLadder(params: {
    ruleset_id: string;
    limit?: number;
    cursor?: string | null;
  }): Promise<PublicLadderResponse> {
    return this.request('/public/ladder', params);
  }

  publicRulesets(): Promise<PublicRulesetsResponse> {
    return this.request('/public/rulesets');
  }

  publicGameAnalytics(gameId: string): Promise<PublicGameAnalyticsResponse> {
    return this.request(`/public/games/${encodeURIComponent(gameId)}/analytics`);
  }

  publicModelAnalytics(agentBuildId: string): Promise<PublicModelAnalyticsResponse> {
    return this.request(`/public/models/${encodeURIComponent(agentBuildId)}/analytics`);
  }

  publicGauntletReport(gauntletId: string): Promise<GauntletReport> {
    return this.request(`/public/gauntlets/${encodeURIComponent(gauntletId)}/report`);
  }

  getGauntletReport(gauntletId: string): Promise<GauntletReport> {
    return this.request(`/gauntlets/${encodeURIComponent(gauntletId)}/report`);
  }

  // ---- admin / read-scoped surface (requires `auth_required=False` or a spectator key)

  listGames(params: {
    limit?: number;
    cursor?: string | null;
    status?: string | null;
    gauntlet_id?: string | null;
    ruleset_id?: string | null;
  } = {}): Promise<CursorPage<GameListEntry>> {
    return this.request('/games', params);
  }

  getGame(gameId: string): Promise<GameDetailResponse> {
    return this.request(`/games/${encodeURIComponent(gameId)}`);
  }

  listGauntlets(params: {
    limit?: number;
    cursor?: string | null;
    status?: string | null;
    league_id?: string | null;
  } = {}): Promise<CursorPage<GauntletListEntry>> {
    return this.request('/gauntlets', params);
  }

  // ---- human multiplayer (Wave 9): cookie-authenticated play channels

  /** Mint a guest principal + human session cookie (US-128). */
  createGuest(): Promise<GuestSummary> {
    return this.mutate('POST', '/human/guest');
  }

  /** Return the current human principal (US-127/128). */
  getHumanMe(): Promise<GuestSummary> {
    return this.request('/human/me');
  }

  /** Set the current session's display name (US-128). */
  setHumanDisplayName(displayName: string): Promise<GuestSummary> {
    return this.mutate('PATCH', '/human/me', { display_name: displayName });
  }

  /** Whether the principal holds a current consent for every document (US-130). */
  getConsentStatus(): Promise<ConsentStatus> {
    return this.request('/human/consent');
  }

  /** Record the one-tap combined consent (TOS + Privacy + 16+) (US-130). */
  postConsent(): Promise<ConsentStatus> {
    return this.mutate('POST', '/human/consent');
  }

  /**
   * Submit a structured action for the caller's seat (US-134). The idempotency
   * key dedupes retries so a network retry never double-votes.
   */
  submitAction(
    gameId: string,
    body: { action: ActionInput; idempotency_key: string }
  ): Promise<ActionResult> {
    return this.mutate('POST', `/human/games/${encodeURIComponent(gameId)}/actions`, body);
  }

  /**
   * Submit a chat message into the buffered hold (US-135). The message is
   * released only after the moderation gate passes; raw text is routed to the
   * out-of-band sidecar, never inline in a hash-chained payload.
   */
  submitChat(
    gameId: string,
    body: { channel: ChatChannel; text: string; idempotency_key: string }
  ): Promise<ChatResult> {
    return this.mutate('POST', `/human/games/${encodeURIComponent(gameId)}/chat`, body);
  }

  /** Submit the post-terminal spot-the-AI guess and return personal accuracy (US-144). */
  submitTuringGuess(
    gameId: string,
    guess: Record<string, SeatGuess>
  ): Promise<TuringGuessResult> {
    return this.mutate('POST', `/human/games/${encodeURIComponent(gameId)}/turing-guess`, {
      guess
    });
  }

  /** Return the caller's own detection accuracy, gated behind their guess (US-144). */
  getTuringGuess(gameId: string): Promise<TuringGuessResult> {
    return this.request(`/human/games/${encodeURIComponent(gameId)}/turing-guess`);
  }

  /** Counts-only composition of a broadcastable game (US-142). */
  publicGameComposition(gameId: string): Promise<PublicCompositionResponse> {
    return this.request(`/public/games/${encodeURIComponent(gameId)}/composition`);
  }

  /** The canonical endgame reveal for a terminal game (US-143). */
  publicGameReveal(gameId: string): Promise<EndgameReveal> {
    return this.request(`/public/games/${encodeURIComponent(gameId)}/reveal`);
  }

  /** Participant-gated reveal for a private terminal human game (US-163). */
  humanGameReveal(gameId: string): Promise<EndgameReveal> {
    return this.request(`/human/games/${encodeURIComponent(gameId)}/reveal`);
  }

  /** Per-human deterministic play stats; gated to the signed-in account (US-145). */
  getHumanStats(rulesetId: string): Promise<HumanPlayerStats> {
    return this.request('/human/stats', { ruleset_id: rulesetId });
  }

  // ---- lobby (US-147/148/149)

  createLobby(body: {
    ruleset_id: string;
    identity_mode?: string;
    theme_pack_id?: string | null;
    prepick_agent_build_ids?: string[];
  }): Promise<LobbySummary> {
    return this.mutate('POST', '/lobbies', body);
  }

  getLobby(lobbyId: string): Promise<LobbySummary> {
    return this.request(`/lobbies/${encodeURIComponent(lobbyId)}`);
  }

  joinLobby(inviteToken: string): Promise<LobbySummary> {
    return this.mutate('POST', `/lobbies/join/${encodeURIComponent(inviteToken)}`);
  }

  getLobbyRoster(lobbyId: string): Promise<LobbyRoster> {
    return this.request(`/lobbies/${encodeURIComponent(lobbyId)}/roster`);
  }

  setLobbyReady(lobbyId: string, ready: boolean): Promise<LobbyRoster> {
    return this.mutate('POST', `/lobbies/${encodeURIComponent(lobbyId)}/ready`, { ready });
  }

  lobbyHeartbeat(lobbyId: string): Promise<LobbyRoster> {
    return this.mutate('POST', `/lobbies/${encodeURIComponent(lobbyId)}/heartbeat`);
  }

  lockLobby(lobbyId: string): Promise<LobbySummary> {
    return this.mutate('POST', `/lobbies/${encodeURIComponent(lobbyId)}/lock`);
  }

  launchLobby(lobbyId: string): Promise<LaunchResponse> {
    return this.mutate('POST', `/lobbies/${encodeURIComponent(lobbyId)}/launch`);
  }

  /**
   * Build the absolute URL of the live-tail SSE stream for a game (US-133). The
   * caller passes it to {@link LiveClient} / `EventSource`; resume-by-sequence
   * uses the `after` query parameter (the SSE `id:` field carries the sequence).
   */
  liveTailUrl(gameId: string, after?: number | null): string {
    return this.buildUrl(`/public/games/${encodeURIComponent(gameId)}/live`, {
      tail: 'true',
      after: after ?? undefined
    });
  }

  /** Build the absolute URL of the caller's per-seat observation SSE stream (US-136). */
  seatObservationUrl(gameId: string): string {
    return this.buildUrl(`/human/games/${encodeURIComponent(gameId)}/observation/stream`);
  }
}

export const API_KEY_STORAGE_KEY = 'padrino-spectator-api-key';

export function loadApiKeyFromSession(): string | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.sessionStorage.getItem(API_KEY_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function saveApiKeyToSession(key: string | null): void {
  if (typeof window === 'undefined') return;
  try {
    if (key === null || key === '') {
      window.sessionStorage.removeItem(API_KEY_STORAGE_KEY);
    } else {
      window.sessionStorage.setItem(API_KEY_STORAGE_KEY, key);
    }
  } catch {
    // sessionStorage may be disabled; ignore.
  }
}

export function resolveBaseUrl(): string {
  const fromEnv = (import.meta.env?.VITE_PADRINO_API_BASE_URL as string | undefined) ?? '';
  if (fromEnv && fromEnv.trim() !== '') return fromEnv.replace(/\/+$/, '');
  return DEFAULT_BASE_URL;
}
