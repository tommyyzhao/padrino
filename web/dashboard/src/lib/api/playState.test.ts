import { describe, expect, it } from 'vitest';
import { applyFrame, applyFrames, emptyPlayState } from './playState';
import type { LiveEventFrame } from './types';

function frame(
  sequence: number,
  event_type: string,
  payload: Record<string, unknown>,
  overrides: Partial<LiveEventFrame> = {}
): LiveEventFrame {
  return {
    schema_version: 'public_event_v1',
    sequence,
    phase: 'DAY_1',
    event_type,
    visibility: 'PUBLIC',
    actor_player_id: null,
    payload,
    prev_event_hash: '',
    event_hash: '',
    ...overrides
  };
}

describe('play-state reducer', () => {
  it('derives the released chat feed from PublicMessageSubmitted frames only', () => {
    const state = applyFrames(emptyPlayState(), [
      frame(1, 'PublicMessageSubmitted', { text: 'hello' }, { actor_player_id: 'p1' }),
      frame(2, 'VoteSubmitted', { target: 'p3', is_abstain: false }, { actor_player_id: 'p2' }),
      frame(3, 'PublicMessageSubmitted', { text: 'world' }, { actor_player_id: 'p2' })
    ]);
    expect(state.chat.map((c) => c.text)).toEqual(['hello', 'world']);
    expect(state.chat[0].public_player_id).toBe('p1');
  });

  it('tracks latest vote per voter and clears votes on a phase change', () => {
    let state = applyFrames(emptyPlayState(), [
      frame(1, 'VoteSubmitted', { target: 'p3', is_abstain: false }, { actor_player_id: 'p1' }),
      frame(2, 'VoteSubmitted', { target: null, is_abstain: true }, { actor_player_id: 'p1' }),
      frame(3, 'VoteSubmitted', { target: 'p1', is_abstain: false }, { actor_player_id: 'p2' })
    ]);
    expect(state.votes).toEqual({ p1: null, p2: 'p1' });

    state = applyFrame(state, frame(4, 'PhaseStarted', {}, { phase: 'NIGHT_1' }));
    expect(state.phase).toBe('NIGHT_1');
    expect(state.votes).toEqual({});
  });

  it('marks a seat dead on PlayerEliminated', () => {
    const state = applyFrames(emptyPlayState(), [
      frame(1, 'VoteSubmitted', { target: 'p3', is_abstain: false }, { actor_player_id: 'p1' }),
      frame(2, 'PlayerEliminated', { public_player_id: 'p1' })
    ]);
    const seat = state.seats.find((s) => s.public_player_id === 'p1');
    expect(seat?.alive).toBe(false);
  });

  it('records the terminal winner once GameTerminated is released', () => {
    const state = applyFrame(
      emptyPlayState(),
      frame(9, 'GameTerminated', { winner: 'TOWN', reason: 'all_mafia_eliminated' })
    );
    expect(state.terminal).toBe(true);
    expect(state.winner).toBe('TOWN');
  });

  it('ignores resume-overlap frames so reconnect folding is idempotent', () => {
    let state = applyFrame(
      emptyPlayState(),
      frame(5, 'PublicMessageSubmitted', { text: 'a' }, { actor_player_id: 'p1' })
    );
    const before = state;
    // A re-delivered frame at or below the cursor is a no-op (same reference).
    state = applyFrame(state, frame(5, 'PublicMessageSubmitted', { text: 'a' }, { actor_player_id: 'p1' }));
    expect(state).toBe(before);
    state = applyFrame(state, frame(4, 'PublicMessageSubmitted', { text: 'old' }, { actor_player_id: 'p1' }));
    expect(state).toBe(before);
    expect(state.chat.length).toBe(1);
  });
});
