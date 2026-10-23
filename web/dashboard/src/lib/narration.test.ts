import { describe, expect, it } from 'vitest';
import { dwellMs, phaseCueFor, phaseLabel, speechFor, type NarratedFrame } from './narration';

const f = (over: Partial<NarratedFrame>): NarratedFrame => ({
  sequence: 1,
  event_type: 'PhaseStarted',
  phase: 'DAY_1',
  actor_player_id: null,
  payload: {},
  ...over
});

describe('narration helpers', () => {
  it('dwell is capped and scales with length', () => {
    expect(dwellMs('')).toBe(1200);
    expect(dwellMs('abcd')).toBe(1380);
    expect(dwellMs('x'.repeat(1000))).toBe(8000);
  });

  it('speaks chat, eliminations and result only', () => {
    expect(speechFor(f({ event_type: 'VoteSubmitted' }))).toBeNull();
    expect(
      speechFor(f({ event_type: 'PublicMessageSubmitted', actor_player_id: 'P01', payload: { text: 'hi' } }))
    ).toMatchObject({ speaker: 'P01', text: 'hi', cue: 'chat' });
    expect(
      speechFor(f({ event_type: 'PlayerEliminated', payload: { public_player_id: 'P02' } }))
    ).toMatchObject({ speaker: null, text: 'P02 has been eliminated.' });
    expect(speechFor(f({ event_type: 'GameTerminated', payload: { winner: 'TOWN' } }))?.text).toBe(
      'The game is over. TOWN wins.'
    );
  });

  it('phase cue only on a new day/vote/night segment', () => {
    expect(phaseCueFor(f({}), 'DAY_1')).toBeNull();
    expect(phaseCueFor(f({}), null)?.text).toBe('Day 1 begins.');
    expect(phaseCueFor(f({}), 'NIGHT_1')?.cue).toBe('phase');
    const round2 = f({ phase: 'DAY_1_DISCUSSION_ROUND_2' });
    expect(phaseCueFor(round2, 'DAY_1_DISCUSSION_ROUND_1')).toBeNull();
    expect(phaseCueFor(f({ phase: 'DAY_1_VOTE' }), 'DAY_1_DISCUSSION_ROUND_2')?.text).toBe(
      'Day 1, voting begins.'
    );
    expect(phaseCueFor(f({ phase: 'NIGHT_1_ACTIONS' }), 'NIGHT_1_MAFIA_DISCUSSION')).toBeNull();
    expect(phaseCueFor(f({ phase: 'END' }), 'DAY_2_VOTE')).toBeNull();
    expect(phaseLabel('NIGHT_1_ACTIONS')).toBe('Night 1 falls.');
  });
});
