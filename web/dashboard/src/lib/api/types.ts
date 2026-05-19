// Hand-written types mirroring the Padrino backend FastAPI response models.
// Source of truth: src/padrino/api/routes/*.py and the OpenAPI schema served
// at /openapi.json. Keep these aligned with the pydantic definitions.

export interface CursorPage<T> {
  items: T[];
  next_cursor: string | null;
  total_estimate?: number;
}

export interface GameListEntry {
  id: string;
  status: string;
  ruleset_id: string;
  gauntlet_id: string | null;
  terminal_result: TerminalResult | null;
  current_phase: string | null;
}

export interface GameDetailResponse {
  id: string;
  status: string;
  terminal_result: TerminalResult | null;
  current_phase: string | null;
  seat_count: number;
}

export interface TerminalResult {
  winner: string;
  reason: string;
  day_terminated?: number;
  [key: string]: unknown;
}

export interface GauntletListEntry {
  id: string;
  league_id: string;
  ruleset_id: string;
  clone_count: number;
  status: string;
  created_at: string;
  completed_at: string | null;
}

export interface PublicEventEntry {
  sequence: number;
  event_type: string;
  phase: string;
  visibility: string;
  actor_player_id: string | null;
  payload: Record<string, unknown>;
  prev_event_hash: string;
  event_hash: string;
}

export interface PublicEventsResponse {
  game_id: string;
  items: PublicEventEntry[];
  next_cursor: string | null;
  total_estimate: number;
}

export interface PublicGameResponse {
  game_id: string;
  ruleset_id: string;
  league_id: string | null;
  gauntlet_id: string | null;
  tip_hash: string;
  signer_fingerprint: string | null;
  verification_status: string;
  bundle: Record<string, unknown>;
}

export interface PublicModelFactionAggregate {
  mu: number;
  sigma: number;
  conservative_score: number;
  games: number;
  wins: number;
  draws: number;
  losses: number;
}

export interface PublicModelEntryResponse {
  model_key: string;
  display_name: string;
  model_provider: string;
  model_name: string;
  model_version: string | null;
  mu: number;
  sigma: number;
  conservative_score: number;
  games: number;
  wins: number;
  draws: number;
  losses: number;
  town: PublicModelFactionAggregate;
  mafia: PublicModelFactionAggregate;
  agent_build_count: number;
}

export interface PublicModelLeaderboardResponse {
  league_id: string;
  ruleset_id: string;
  rating_model: string;
  cache_tag: string;
  entries: PublicModelEntryResponse[];
  next_cursor: string | null;
  total_estimate: number;
}

export interface PublicLeaderboardEntryResponse {
  entity_id: string;
  display_name: string;
  model_provider: string;
  model_name: string;
  model_version: string | null;
  prompt_version: string;
  games: number;
  wins: number;
  draws: number;
  losses: number;
  mu: number;
  sigma: number;
  conservative_score: number;
}

export interface PublicLeaderboardResponse {
  ruleset_id: string;
  gauntlet_id: string | null;
  rating_model: string;
  cache_tag: string;
  entries: PublicLeaderboardEntryResponse[];
  next_cursor: string | null;
  total_estimate: number;
}

export type FactionTab = 'global' | 'town' | 'mafia';
