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

export interface PublicRatingCardResponse {
  card_id: string;
  section: 'canonical' | 'experimental' | 'humans_included';
  section_label: string;
  context_kind: string;
  context_label: string;
  ruleset_id: string;
  entity_id: string;
  display_name: string;
  model_provider: string;
  model_name: string;
  model_version: string | null;
  prompt_version: string;
  scope_type: string;
  scope_value: string;
  metric: 'openskill_conservative' | 'solo_success_rate';
  metric_label: string;
  score: number;
  rank: number | null;
  provisional: boolean;
  provisional_reason: string | null;
  sample_count: number;
  games: number | null;
  attempts: number | null;
  successes: number | null;
  mu: number | null;
  sigma: number | null;
  conservative_score: number | null;
  mean_success_rate: number | null;
  credible_interval_low: number | null;
  credible_interval_high: number | null;
}

export interface PublicLeaderboardResponse {
  ruleset_id: string | null;
  gauntlet_id: string | null;
  rating_model: string;
  cache_tag: string;
  entries: PublicLeaderboardEntryResponse[];
  canonical_cards: PublicRatingCardResponse[];
  faction_cards: PublicRatingCardResponse[];
  experimental_cards: PublicRatingCardResponse[];
  human_cards: PublicRatingCardResponse[];
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

export interface PublicRulesetEntry {
  ruleset_id: string;
  label: string;
  player_count: number;
  rating_context_kind: string;
  is_canonical: boolean;
}

export interface PublicRulesetsResponse {
  items: PublicRulesetEntry[];
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

// ---------------------------------------------------------------------------
// Human multiplayer (Wave 9) — live play transport, lobby, reveal, guess, stats.
// Source of truth: src/padrino/api/routes/human.py, lobbies.py, public.py and
// src/padrino/core/{reveal,composition}.py. Keep aligned with the pydantic
// definitions. These surfaces are identity-blind in anonymous mode: a live
// frame never carries a human-vs-AI / model-identity marker before the reveal.

/** Counts-only composition disclosure (US-126/142): never a per-seat map. */
export interface CompositionSummary {
  human_count: number;
  ai_count: number;
  total: number;
}

/** A human principal summary (guest or account); no PII beyond a display name. */
export interface GuestSummary {
  principal_id: string;
  kind: string;
  display_name: string | null;
}

/** Whether the current human holds a current consent for every legal document. */
export interface ConsentStatus {
  consented: boolean;
  required_versions: Record<string, string>;
}

/** A structured action a human submits for their seat (US-134). */
export interface ActionInput {
  type: string;
  target?: string | null;
}

/** The accepted (or idempotently replayed) action submission (US-134). */
export interface ActionResult {
  accepted: boolean;
  public_player_id: string;
  phase: string;
  action_type: string;
  target: string | null;
  idempotent_replay: boolean;
}

export type ChatChannel = 'PUBLIC' | 'PRIVATE';

/** The accepted (or idempotently replayed) chat submission (US-135). */
export interface ChatResult {
  accepted: boolean;
  public_player_id: string;
  phase: string;
  channel: string;
  status: string;
  idempotent_replay: boolean;
}

export type SeatGuess = 'HUMAN' | 'AI';

/** The caller's personal spot-the-AI detection accuracy (US-144). */
export interface TuringGuessResult {
  guesser_public_id: string;
  total: number;
  correct: number;
  accuracy: string;
  idempotent_replay: boolean;
}

/** Exact model identity for an AI (or taken-over) seat at the reveal (US-143). */
export interface RevealModel {
  provider: string;
  model_name: string;
  model_version: string | null;
  agent_build_id: string;
  display_name: string | null;
}

/** The full per-seat truth disclosed at the endgame reveal (US-143). */
export interface SeatReveal {
  public_player_id: string;
  seat_index: number;
  is_human: boolean;
  role: string;
  faction: string;
  alive: boolean;
  takeover_provenance: string;
  taken_over_at_phase: string | null;
  model: RevealModel | null;
}

/** The canonical endgame reveal: every seat's full truth (US-143). */
export interface EndgameReveal {
  game_id: string;
  ruleset_id: string;
  winner: string | null;
  seats: SeatReveal[];
}

/** Counts-only composition of a broadcastable game (US-142). */
export interface PublicCompositionResponse {
  game_id: string;
  ruleset_id: string;
  composition: CompositionSummary;
}

/** Member-scoped view of a lobby: config + counts-only composition (US-147). */
export interface LobbySummary {
  id: string;
  ruleset_id: string;
  identity_mode: string;
  theme_pack_id: string | null;
  stakes: string;
  ranked: boolean;
  integrity_acknowledged: boolean;
  status: string;
  invite_token: string;
  host_principal_id: string;
  league_id: string;
  game_id: string | null;
  member_count: number;
  composition: CompositionSummary;
}

/** Identity-blind roster entry: no principal id, seat_kind, or human/AI map. */
export interface RosterMember {
  member_id: string;
  is_host: boolean;
  ready: boolean;
  present: boolean;
}

/** A member-scoped roster + counts-only composition (US-148). */
export interface LobbyRoster {
  id: string;
  status: string;
  member_count: number;
  composition: CompositionSummary;
  members: RosterMember[];
}

/** Result of a launch handoff: the materialized game and lobby status (US-149). */
export interface LaunchResponse {
  lobby_id: string;
  game_id: string;
  status: string;
  created: boolean;
}

/** Result of a solo instant-match handoff (US-278): route to the new game. */
export interface HumanMatchResponse {
  game_id: string;
}

/** Per-human deterministic play stats (US-145); no leaderboard / ELO in v1. */
export interface HumanPlayerStats {
  ruleset_id: string;
  principal_id: string;
  games: number;
  wins: number;
  draws: number;
  losses: number;
  role_win_rates: RoleWinRateAnalytics[];
  survival_rate: number;
  voting_accuracy: VotingAccuracyAnalytics;
  detection_accuracy: string;
}

/** The caller's own postgame spot-the-AI result when they have submitted it. */
export interface HumanGameSpotTheAi {
  total: number;
  correct: number;
  accuracy: string;
}

/** One completed casual human-lane game in the caller's private history. */
export interface HumanGameHistoryEntry {
  game_id: string;
  ruleset_id: string;
  ended_at: string;
  result: 'WIN' | 'LOSS' | 'DRAW' | 'UNKNOWN';
  winner: string | null;
  role: string;
  spot_the_ai: HumanGameSpotTheAi | null;
  reveal_path: string;
}

// ---- live transport frames (SSE-out) -------------------------------------

/** One released PUBLIC frame from the live-tail SSE (public_event_v1, US-133). */
export interface LiveEventFrame {
  schema_version: string;
  sequence: number;
  phase: string;
  event_type: string;
  visibility: string;
  actor_player_id: string | null;
  payload: Record<string, unknown>;
  prev_event_hash: string;
  event_hash: string;
}

/** SSE discriminators for the per-seat observation stream (US-136). */
export type SeatStreamFrameType = 'observation' | 'phase_deadline';

/**
 * The legal action types + targets for the seat in the current phase (US-134).
 * Mirrors `padrino.core.engine.legal_actions.LegalActions`. Drives the
 * legal-action-gated action / vote / night panels on the play surface (US-155).
 */
export interface LegalActionsView {
  allowed_action_types: string[];
  legal_targets: string[];
  action_descriptions?: Record<string, string>;
}

/** Non-leaky notice shown to a player returning after the server resumed their seat. */
export interface ReturnNoticeView {
  kind: 'away_resuming';
  message: string;
}

/** The seat's own identity-mode-aware observation projection frame (US-136). */
export interface SeatObservationFrame {
  type: 'observation';
  phase?: string;
  alive_players?: string[];
  legal_actions?: LegalActionsView;
  return_notice?: ReturnNoticeView;
  [key: string]: unknown;
}

/** Transport-only phase-deadline frame; never in the hash-chained log (US-136). */
export interface PhaseDeadlineFrame {
  type: 'phase_deadline';
  phase: string;
  deadline_at: string | null;
}

export type SeatStreamFrame = SeatObservationFrame | PhaseDeadlineFrame;
