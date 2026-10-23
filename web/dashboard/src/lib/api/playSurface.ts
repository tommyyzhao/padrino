// In-game play surface helpers (Wave 9, US-155).
//
// Pure, framework-free helpers behind the `/play/[gameId]` surface so the
// load-bearing UI logic is unit tested without the Svelte runes runtime:
//
//   * the buffered chat composer's pending/blocked/released lifecycle,
//   * a NON-PRECISE phase countdown bucket (the play header shows a coarse
//     "time left" that cannot leak a per-seat timing signal, AGENTS.md rule 7),
//   * the seat-sprite asset URL resolver (identity-blind: the sprite key is
//     resolved server-side; this only builds the static asset URL).
//
// Identity-blindness is upstream of every helper here: the chat feed is fed only
// by RELEASED `public_event_v1` frames (the buffered hold + symmetric release
// delay live server-side, US-138/140) and the seat sprite key is an
// anonymous-mode role-agnostic archetype (US-152). These helpers therefore never
// see — and can never surface — a human-vs-AI or model-identity marker before
// the endgame reveal.

import type { LegalActionsView } from './types';

/** A public seat fact suitable for the pre-reveal table. */
export interface TableSeat {
  public_player_id: string;
  alive: boolean;
}

/** The default art pack when a legacy game has no persisted visual theme. */
export const DEFAULT_PLAY_THEME = 'classic_noir';

const ANONYMOUS_ARCHETYPES = ['archetype_a', 'archetype_b', 'archetype_c', 'archetype_d'] as const;

/** The lifecycle of one human chat submission as the composer tracks it. */
export type ComposerStatus = 'idle' | 'pending' | 'released' | 'blocked' | 'error';

/**
 * Map a backend chat-submission `status` to the composer lifecycle.
 *
 * The chat POST parks the message in the buffer hold (`HELD`) and the moderation
 * gate later flips it to `RELEASED` or `BLOCKED` (US-135/140). `HELD` surfaces as
 * `pending` — the composer shows the message as in-flight until a released frame
 * carrying it arrives over the live-tail stream.
 */
export function composerStatusFromChat(status: string): ComposerStatus {
  switch (status) {
    case 'RELEASED':
      return 'released';
    case 'BLOCKED':
      return 'blocked';
    case 'HELD':
      return 'pending';
    default:
      return 'pending';
  }
}

/** A coarse, non-precise "time left" bucket for the phase countdown header. */
export type CountdownBucket = 'none' | 'plenty' | 'soon' | 'ending';

/** Visual mood resolved only from the server's committed phase/terminal facts. */
export type PhaseAtmosphere = 'neutral' | 'day' | 'night' | 'terminal';

/**
 * Return the play-table atmosphere without inspecting local mechanics or any
 * controller/model metadata. Terminal is a committed `GameTerminated` fact.
 */
export function phaseAtmosphere(phase: string, terminal: boolean): PhaseAtmosphere {
  if (terminal) return 'terminal';
  const normalized = phase.toUpperCase();
  if (normalized.startsWith('NIGHT_')) return 'night';
  if (normalized.startsWith('DAY_')) return 'day';
  return 'neutral';
}

/** Device and user facts that may permit a deliberately conservative cue. */
export interface SoundCuePreferences {
  muted: boolean;
  prefersReducedMotion: boolean;
  prefersReducedData: boolean;
}

/**
 * Cues are opt-in: reduced-motion and reduced-data preferences always win over
 * the persisted setting, while visual equivalents remain available.
 */
export function shouldPlaySoundCue(preferences: SoundCuePreferences): boolean {
  return !(
    preferences.muted ||
    preferences.prefersReducedMotion ||
    preferences.prefersReducedData
  );
}

/**
 * Bucket the seconds remaining until the phase deadline into a COARSE label.
 *
 * The header deliberately shows a non-precise countdown: a precise per-seat
 * timer could leak a timing signal about when a seat acted (AGENTS.md rule 7),
 * so the surface only ever renders the bucket, never the exact seconds.
 *
 *   * `none`   — no deadline known (or already elapsed);
 *   * `ending` — under 15s;
 *   * `soon`   — under 60s;
 *   * `plenty` — otherwise.
 */
export function countdownBucket(secondsRemaining: number | null): CountdownBucket {
  if (secondsRemaining === null || secondsRemaining <= 0) return 'none';
  if (secondsRemaining < 15) return 'ending';
  if (secondsRemaining < 60) return 'soon';
  return 'plenty';
}

/** Seconds remaining until an ISO-8601 deadline, or null if absent/elapsed. */
export function secondsUntil(deadlineIso: string | null, nowMs: number): number | null {
  if (deadlineIso === null) return null;
  const deadlineMs = Date.parse(deadlineIso);
  if (Number.isNaN(deadlineMs)) return null;
  const remaining = Math.round((deadlineMs - nowMs) / 1000);
  return remaining > 0 ? remaining : null;
}

/** A human-readable, non-precise label for a countdown bucket. */
export function countdownLabel(bucket: CountdownBucket): string {
  switch (bucket) {
    case 'ending':
      return 'Ending soon';
    case 'soon':
      return 'A little time left';
    case 'plenty':
      return 'Plenty of time';
    case 'none':
      return 'No timer';
  }
}

/** Whether the seat may submit a given action type in the current phase. */
export function actionAllowed(legal: LegalActionsView | null, actionType: string): boolean {
  if (legal === null) return false;
  return legal.allowed_action_types.includes(actionType);
}

/** Human-readable label for a server-provided action type. */
export function actionTypeLabel(actionType: string): string {
  return actionType
    .split('_')
    .filter((part) => part.length > 0)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1).toLowerCase())
    .join(' ');
}

/**
 * Build a readable, identity-blind table name from the public seat token.
 *
 * This intentionally consults only the public token: it cannot encode a role,
 * faction, human/AI controller, model, or lobby identity.
 */
export function seatPseudonym(publicPlayerId: string): string {
  const token = publicPlayerId.replace(/[^a-z0-9]/gi, '').slice(0, 4).toUpperCase();
  return `Cipher ${token || 'SEAT'}`;
}

/**
 * Select one role-agnostic portrait key from a public seat token.
 *
 * The browser may use this only for the live anonymous table. It mirrors the
 * server-side invariant in spirit: the key is a deterministic function of the
 * public seat label alone and never of any hidden/controller data.
 */
export function anonymousPortraitKey(publicPlayerId: string): (typeof ANONYMOUS_ARCHETYPES)[number] {
  let hash = 0;
  for (let index = 0; index < publicPlayerId.length; index += 1) {
    hash = (hash * 31 + publicPlayerId.charCodeAt(index)) >>> 0;
  }
  return ANONYMOUS_ARCHETYPES[hash % ANONYMOUS_ARCHETYPES.length] ?? 'archetype_a';
}

/**
 * Combine public stream seats with a caller's own observed roster.
 *
 * The observation is allowed to reveal the full alive/dead roster to its owner,
 * while the public stream is the source for released eliminations. Death is
 * monotonic: a released or observed dead seat can never be resurrected by a
 * later stale observation frame.
 */
export function mergeTableSeats(
  releasedSeats: readonly TableSeat[],
  alivePlayerIds: readonly string[],
  deadPlayerIds: readonly string[],
  ownSeatId: string | null
): TableSeat[] {
  const orderedIds: string[] = [];
  const seats = new Map<string, TableSeat>();

  const remember = (publicPlayerId: string, alive: boolean): void => {
    if (publicPlayerId === '') return;
    const existing = seats.get(publicPlayerId);
    if (existing === undefined) {
      orderedIds.push(publicPlayerId);
      seats.set(publicPlayerId, { public_player_id: publicPlayerId, alive });
      return;
    }
    if (!existing.alive || alive) return;
    seats.set(publicPlayerId, { public_player_id: publicPlayerId, alive: false });
  };

  for (const seat of releasedSeats) remember(seat.public_player_id, seat.alive);
  for (const publicPlayerId of alivePlayerIds) remember(publicPlayerId, true);
  for (const publicPlayerId of deadPlayerIds) remember(publicPlayerId, false);
  if (ownSeatId !== null) remember(ownSeatId, true);

  return orderedIds.flatMap((publicPlayerId) => {
    const seat = seats.get(publicPlayerId);
    return seat === undefined ? [] : [seat];
  });
}

/** A short player-facing description for a currently legal action type. */
export function actionTypeDescription(
  legal: LegalActionsView | null,
  actionType: string | null
): string | null {
  if (legal === null || actionType === null) return null;
  if (!legal.allowed_action_types.includes(actionType)) return null;

  const provided = legal.action_descriptions?.[actionType]?.trim();
  if (provided) return provided;

  const label = actionTypeLabel(actionType);
  if (legal.legal_targets.length > 0) {
    return `${label} one legal target. This option is available because the current ruleset and phase allow it.`;
  }
  return `${label} for this phase. This option is available because the current ruleset and phase allow it.`;
}

/** The server-provided targeted non-vote action for the current seat, if any. */
export function nightActionType(legal: LegalActionsView | null): string | null {
  if (legal === null) return null;
  if (legal.allowed_action_types.length === 0) return null;
  if (isVotePhase(legal)) return null;
  if (legal.legal_targets.length === 0) return null;
  return legal.allowed_action_types[0] ?? null;
}

/** Whether the seat's current phase is a night-action phase (role-conditional). */
export function isNightActionPhase(legal: LegalActionsView | null): boolean {
  return nightActionType(legal) !== null;
}

/** Whether the seat's current phase is the day-vote phase. */
export function isVotePhase(legal: LegalActionsView | null): boolean {
  return actionAllowed(legal, 'VOTE');
}

/**
 * Build the static asset URL for a seat sprite key within a theme pack.
 *
 * The sprite KEY is resolved server-side (anonymous mode hands back only a
 * role-agnostic archetype, US-152); this only assembles the immutable asset URL.
 * A missing theme pack or key falls back to the deterministic placeholder.
 */
export function spriteUrl(
  baseUrl: string,
  themePackId: string | null,
  spriteKey: string | null
): string {
  const root = baseUrl.replace(/\/+$/, '');
  if (themePackId === null || spriteKey === null || spriteKey === 'placeholder') {
    return `${root}/public/sprites/placeholder`;
  }
  return `${root}/public/sprites/${encodeURIComponent(themePackId)}/${encodeURIComponent(spriteKey)}`;
}
