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
    ).toMatchObject({ speaker: null, text: 'P02 was eliminated.' });
    expect(speechFor(f({ event_type: 'GameTerminated', payload: { winner: 'TOWN' } }))?.text).toBe(
      'Game over. Winner: TOWN.'
    );
  });

  it('phase cue only on change', () => {
    expect(phaseCueFor(f({}), 'DAY_1')).toBeNull();
    expect(phaseCueFor(f({}), 'NIGHT_1')?.cue).toBe('phase');
    expect(phaseLabel('NIGHT_1_ACTIONS')).toBe('Night 1 actions');
  });
});
