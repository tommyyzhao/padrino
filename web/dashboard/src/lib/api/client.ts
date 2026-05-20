import type {
  CursorPage,
  GameDetailResponse,
  GameListEntry,
  GauntletListEntry,
  GauntletReport,
  PublicEventsResponse,
  PublicGameResponse,
  PublicLeaderboardResponse,
  PublicModelLeaderboardResponse
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
  fetchImpl?: typeof fetch;
}

export class PadrinoClient {
  readonly baseUrl: string;
  private apiKey: string | null;
  private readonly fetchImpl: typeof fetch;

  constructor(options: PadrinoClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? DEFAULT_BASE_URL).replace(/\/+$/, '');
    this.apiKey = options.apiKey ?? null;
    this.fetchImpl = options.fetchImpl ?? fetch.bind(globalThis);
  }

  setApiKey(key: string | null): void {
    this.apiKey = key;
  }

  hasApiKey(): boolean {
    return this.apiKey !== null && this.apiKey !== '';
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

  private async request<T>(path: string, params?: Record<string, string | number | null | undefined>): Promise<T> {
    const headers: Record<string, string> = { Accept: 'application/json' };
    if (this.apiKey) {
      headers.Authorization = `Bearer ${this.apiKey}`;
    }
    const response = await this.fetchImpl(this.buildUrl(path, params), { headers });
    if (!response.ok) {
      let detail: unknown = null;
      try {
        detail = await response.json();
      } catch {
        // ignore non-JSON error bodies
      }
      throw new PadrinoApiError(response.status, detail);
    }
    return (await response.json()) as T;
  }

  // ---- public (unauthenticated when padrino_public_leaderboard_anonymous=True)

  publicLeaderboard(params: {
    ruleset_id: string;
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
