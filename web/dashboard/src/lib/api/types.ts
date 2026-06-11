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

export interface PublicLiveGameEntry {
  game_id: string;
  ruleset_id: string;
  current_phase: string | null;
  players_alive: number;
}

export interface PublicLiveIndexResponse {
  items: PublicLiveGameEntry[];
  total: number;
}

export interface PublicRecentGameEntry {
  game_id: string;
  ruleset_id: string;
  current_phase: string | null;
  terminal_result: TerminalResult | null;
}

export interface PublicRecentIndexResponse {
  items: PublicRecentGameEntry[];
  next_cursor: string | null;
  total_estimate: number;
}

export interface PublicLadderEntry {
  agent_build_id: string;
  display_name: string;
  version: string;
  ordinal: number;
  provisional: boolean;
  games: number;
  last_game_at: string | null;
}

export interface PublicLadderResponse {
  ruleset_id: string;
  entries: PublicLadderEntry[];
  next_cursor: string | null;
  total_estimate: number;
}

export interface VotingAccuracyAnalytics {
  total_votes: number;
  accurate_votes: number;
  rate: number;
}

export interface SurvivalPointAnalytics {
  role: string;
  day: number;
  alive_count: number;
  total_count: number;
  fraction: number;
}

export interface RoleWinRateAnalytics {
  role: string;
  wins: number;
  games: number;
  rate: number;
}

export interface ClaimRecordAnalytics {
  player_id: string;
  claimed_role: string;
  sequence: number;
  phase: string;
}

export interface CounterClaimGroupAnalytics {
  claimed_role: string;
  claimants: string[];
}

export interface PublicGameAnalyticsResponse {
  game_id: string;
  ruleset_id: string;
  winner: string | null;
  voting_accuracy: VotingAccuracyAnalytics;
  survival_curve: SurvivalPointAnalytics[];
  role_win_rates: RoleWinRateAnalytics[] | null;
  claims: ClaimRecordAnalytics[];
  counter_claims: CounterClaimGroupAnalytics[];
}

export interface PublicModelAnalyticsResponse {
  agent_build_id: string;
  ruleset_id: string;
  version: string;
  games_played: number;
  role_win_rates: RoleWinRateAnalytics[];
  voting_accuracy: VotingAccuracyAnalytics;
  survival_curve: SurvivalPointAnalytics[];
  computed_at: string;
}

export type FactionTab = 'global' | 'town' | 'mafia';

// Mirrors padrino.gauntlets.evaluation.CIBand.
export interface CIBand {
  point: number;
  lower: number;
  upper: number;
}

export interface FactionWinRate {
  faction: string;
  wins: number;
  games: number;
  rate: CIBand;
}

export interface RoleFamilyBreakdown {
  role_family: string;
  games: number;
  wins: number;
  draws: number;
  losses: number;
  win_rate: CIBand;
}

export interface RatingDelta {
  agent_build_id: string;
  scope_type: string;
  scope_value: string;
  games_in_gauntlet: number;
  pre_mu: number;
  pre_sigma: number;
  post_mu: number;
  post_sigma: number;
  delta_mu: number;
  delta_sigma: number;
}

export interface GauntletReport {
  gauntlet_id: string;
  status: string;
  ruleset_id: string;
  clone_count: number;
  games_total: number;
  games_completed: number;
  faction_win_counts: Record<string, number>;
  faction_win_rates: FactionWinRate[];
  role_family_breakdown: RoleFamilyBreakdown[];
  average_days_to_terminal: number;
  average_actions_per_seat: number;
  rating_deltas: RatingDelta[];
}
