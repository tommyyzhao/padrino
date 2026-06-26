import { describe, expect, it } from 'vitest';
import {
  applyFrame,
  applyFrames,
  derivePhaseBanner,
  deriveVoteTally,
  emptyPlayState
} from './playState';
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

  it('derives voter target rows and running target counts from VoteSubmitted frames', () => {
    let state = applyFrames(emptyPlayState(), [
      frame(1, 'PhaseStarted', {}, { phase: 'DAY_1_VOTE' }),
      frame(2, 'VoteSubmitted', { target: 'p3', is_abstain: false }, { actor_player_id: 'p1', phase: 'DAY_1_VOTE' }),
      frame(3, 'VoteSubmitted', { target: 'p3', is_abstain: false }, { actor_player_id: 'p2', phase: 'DAY_1_VOTE' }),
      frame(4, 'VoteSubmitted', { target: null, is_abstain: true }, { actor_player_id: 'p4', phase: 'DAY_1_VOTE' })
    ]);

    expect(deriveVoteTally(state.votes)).toEqual({
      rows: [
        { voter: 'p1', target: 'p3' },
        { voter: 'p2', target: 'p3' },
        { voter: 'p4', target: null }
      ],
      counts: [{ target: 'p3', count: 2 }]
    });

    state = applyFrame(
      state,
      frame(5, 'VoteSubmitted', { target: 'p2', is_abstain: false }, { actor_player_id: 'p4', phase: 'DAY_1_VOTE' })
    );

    expect(deriveVoteTally(state.votes)).toEqual({
      rows: [
        { voter: 'p1', target: 'p3' },
        { voter: 'p2', target: 'p3' },
        { voter: 'p4', target: 'p2' }
      ],
      counts: [
        { target: 'p3', count: 2 },
        { target: 'p2', count: 1 }
      ]
    });
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

  it('derives phase-transition banners from PhaseStarted frames', () => {
    expect(derivePhaseBanner('', 'DAY_1_DISCUSSION_ROUND_1', 1)).toEqual({
      phase: 'DAY_1_DISCUSSION_ROUND_1',
      sequence: 1,
      kind: 'day',
      message: 'Day breaks'
    });
    expect(derivePhaseBanner('DAY_1_VOTE', 'NIGHT_1_ACTIONS', 9)).toEqual({
      phase: 'NIGHT_1_ACTIONS',
      sequence: 9,
      kind: 'night',
      message: 'Night falls'
    });
    expect(derivePhaseBanner('NIGHT_1_ACTIONS', 'DAY_2_VOTE', 10)).toEqual({
      phase: 'DAY_2_VOTE',
      sequence: 10,
      kind: 'day',
      message: 'Day breaks'
    });
    expect(derivePhaseBanner('DAY_2_VOTE', 'GAME_OVER', 11)).toBeNull();
  });

  it('stores the latest phase banner when a phase-start transition arrives', () => {
    const state = applyFrames(emptyPlayState(), [
      frame(1, 'PhaseStarted', {}, { phase: 'DAY_1_VOTE' }),
      frame(2, 'PhaseStarted', {}, { phase: 'NIGHT_1_ACTIONS' })
    ]);

    expect(state.phaseBanner).toEqual({
      phase: 'NIGHT_1_ACTIONS',
      sequence: 2,
      kind: 'night',
      message: 'Night falls'
    });
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
