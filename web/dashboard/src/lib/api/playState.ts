// Pure play-state reducer (Wave 9, US-153).
//
// The play-session store derives the board, the current-phase votes, and the
// chat feed from the RELEASED frame stream — the live-tail SSE delivers only
// already-released PUBLIC `public_event_v1` frames (the buffered hold + the
// symmetric release delay live server-side, US-138/140). This module is the
// pure, framework-free reducer behind the Svelte 5 runes store: a fresh state
// folds one released frame at a time, so the same logic is exhaustively unit
// tested without the runes runtime and reused verbatim by the `.svelte.ts`
// wrapper.
//
// Identity-blindness is upstream: a released frame is already projected through
// the identity-blind `public_event_v1` contract (FORBIDDEN_PAYLOAD_KEYS), so
// this reducer never sees — and therefore can never surface — a human-vs-AI or
// model-identity marker before the endgame reveal.

import type { LiveEventFrame } from './types';

/** One released chat line in the order it was released. */
export interface ReleasedChatLine {
  sequence: number;
  phase: string;
  /** The speaking seat (null for an un-attributed system line). */
  public_player_id: string | null;
  text: string;
}

/** A seat's known board status, derived from released frames only. */
export interface BoardSeat {
  public_player_id: string;
  alive: boolean;
}

/** The derived, identity-blind play state. */
export interface PlayState {
  /** Highest released sequence folded so far (the resume cursor). */
  lastSequence: number | null;
  /** The current logical phase id, taken from the latest frame. */
  phase: string;
  /** Seats seen so far, keyed by public_player_id, in first-seen order. */
  seats: BoardSeat[];
  /** Latest vote per voter within the CURRENT phase (cleared on phase change). */
  votes: Record<string, string | null>;
  /** The released chat feed in release order. */
  chat: ReleasedChatLine[];
  /** The terminal winner once GameTerminated is released, else null. */
  winner: string | null;
  /** Whether the terminal frame has been released. */
  terminal: boolean;
}

export function emptyPlayState(): PlayState {
  return {
    lastSequence: null,
    phase: '',
    seats: [],
    votes: {},
    chat: [],
    winner: null,
    terminal: false
  };
}

function ensureSeat(seats: BoardSeat[], publicPlayerId: string): BoardSeat[] {
  if (seats.some((s) => s.public_player_id === publicPlayerId)) return seats;
  return [...seats, { public_player_id: publicPlayerId, alive: true }];
}

function asString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

/**
 * Fold one released frame into the play state, returning a NEW state (immutable
 * update so the runes store can assign it and trigger reactivity). A frame whose
 * sequence is not strictly greater than `lastSequence` is ignored, so replaying
 * a resume overlap is a no-op (gap-free AND duplicate-free).
 */
export function applyFrame(state: PlayState, frame: LiveEventFrame): PlayState {
  if (state.lastSequence !== null && frame.sequence <= state.lastSequence) {
    return state;
  }

  const phaseChanged = frame.phase !== '' && frame.phase !== state.phase;
  let seats = state.seats;
  let votes = phaseChanged ? {} : state.votes;
  let chat = state.chat;
  let winner = state.winner;
  let terminal = state.terminal;

  const actor = frame.actor_player_id;
  if (actor) {
    seats = ensureSeat(seats, actor);
  }

  switch (frame.event_type) {
    case 'VoteSubmitted': {
      if (actor) {
        const isAbstain = frame.payload.is_abstain === true;
        const target = asString(frame.payload.target);
        votes = { ...votes, [actor]: isAbstain ? null : target };
      }
      break;
    }
    case 'PublicMessageSubmitted': {
      const text = asString(frame.payload.text);
      if (text !== null) {
        chat = [
          ...chat,
          { sequence: frame.sequence, phase: frame.phase, public_player_id: actor, text }
        ];
      }
      break;
    }
    case 'PlayerEliminated': {
      const eliminated = asString(frame.payload.public_player_id);
      if (eliminated !== null) {
        seats = ensureSeat(seats, eliminated).map((s) =>
          s.public_player_id === eliminated ? { ...s, alive: false } : s
        );
      }
      break;
    }
    case 'GameTerminated': {
      terminal = true;
      winner = asString(frame.payload.winner);
      break;
    }
    default:
      break;
  }

  return {
    lastSequence: frame.sequence,
    phase: frame.phase !== '' ? frame.phase : state.phase,
    seats,
    votes,
    chat,
    winner,
    terminal
  };
}

/** Fold a batch of released frames in order. */
export function applyFrames(state: PlayState, frames: readonly LiveEventFrame[]): PlayState {
  let next = state;
  for (const frame of frames) {
    next = applyFrame(next, frame);
  }
  return next;
}
