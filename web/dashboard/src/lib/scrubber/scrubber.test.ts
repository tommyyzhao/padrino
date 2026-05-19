import { describe, expect, it } from 'vitest';
import type { PublicEventEntry } from '$lib/api/types';
import {
  currentGroup,
  groupEventsByPhase,
  initScrubber,
  jumpTo,
  next,
  prev,
  projectEventForPublic,
  ROLE_REVEAL_EVENT_TYPES
} from './index';

function event(partial: Partial<PublicEventEntry>): PublicEventEntry {
  return {
    sequence: 0,
    event_type: 'NoOp',
    phase: 'DAY_1_VOTE',
    visibility: 'PUBLIC',
    actor_player_id: null,
    payload: {},
    prev_event_hash: '',
    event_hash: '',
    ...partial
  };
}

describe('groupEventsByPhase', () => {
  it('preserves first-seen phase order and keeps events in original order within a group', () => {
    const events = [
      event({ phase: 'DAY_1_VOTE', sequence: 1, event_hash: 'h1' }),
      event({ phase: 'NIGHT_1_ACTIONS', sequence: 2, event_hash: 'h2' }),
      event({ phase: 'DAY_1_VOTE', sequence: 3, event_hash: 'h3' }),
      event({ phase: 'DAY_2_VOTE', sequence: 4, event_hash: 'h4' })
    ];
    const groups = groupEventsByPhase(events);
    expect(groups.map((g) => g.phase)).toEqual([
      'DAY_1_VOTE',
      'NIGHT_1_ACTIONS',
      'DAY_2_VOTE'
    ]);
    expect(groups[0].events.map((e) => e.sequence)).toEqual([1, 3]);
    expect(groups[0].index).toBe(0);
    expect(groups[1].index).toBe(1);
  });

  it('uses UNKNOWN phase for events with empty phase strings', () => {
    const groups = groupEventsByPhase([event({ phase: '' })]);
    expect(groups[0].phase).toBe('UNKNOWN');
  });
});

describe('scrubber navigation', () => {
  const events = [
    event({ phase: 'DAY_1_VOTE', sequence: 1, event_hash: 'h1' }),
    event({ phase: 'NIGHT_1_ACTIONS', sequence: 2, event_hash: 'h2' }),
    event({ phase: 'DAY_2_VOTE', sequence: 3, event_hash: 'h3' })
  ];

  it('initializes at index 0 with non-empty events', () => {
    const state = initScrubber(events);
    expect(state.currentIndex).toBe(0);
    expect(currentGroup(state)?.phase).toBe('DAY_1_VOTE');
  });

  it('returns -1 index for empty input', () => {
    const state = initScrubber([]);
    expect(state.currentIndex).toBe(-1);
    expect(currentGroup(state)).toBeNull();
  });

  it('clamps next/prev at boundaries', () => {
    let state = initScrubber(events);
    state = next(state);
    expect(state.currentIndex).toBe(1);
    state = next(state);
    expect(state.currentIndex).toBe(2);
    state = next(state); // already at last
    expect(state.currentIndex).toBe(2);
    state = prev(state);
    expect(state.currentIndex).toBe(1);
    state = prev(state);
    expect(state.currentIndex).toBe(0);
    state = prev(state); // already at first
    expect(state.currentIndex).toBe(0);
  });

  it('jumpTo accepts both index and phase name and clamps', () => {
    const state = initScrubber(events);
    expect(jumpTo(state, 2).currentIndex).toBe(2);
    expect(jumpTo(state, 'DAY_2_VOTE').currentIndex).toBe(2);
    expect(jumpTo(state, 99).currentIndex).toBe(2);
    expect(jumpTo(state, -5).currentIndex).toBe(0);
    expect(jumpTo(state, 'UNKNOWN_PHASE').currentIndex).toBe(state.currentIndex);
  });

  it('returns immutable copies', () => {
    const state = initScrubber(events);
    const after = next(state);
    expect(after).not.toBe(state);
    expect(state.currentIndex).toBe(0);
  });
});

describe('projectEventForPublic', () => {
  it('redacts role-revealing payloads while not terminal', () => {
    const ev = event({
      event_type: 'RolesAssigned',
      payload: { assignments: [{ public_player_id: 'p1', role: 'MAFIA', faction: 'MAFIA' }] }
    });
    const projected = projectEventForPublic(ev, false);
    expect(projected.payload).toEqual({ redacted: true, reason: 'role_hidden_until_terminal' });
  });

  it('passes role-revealing payloads through when terminal', () => {
    const ev = event({
      event_type: 'GameTerminated',
      payload: { winner: 'TOWN', reason: 'ALL_MAFIA_ELIMINATED' }
    });
    expect(projectEventForPublic(ev, true)).toBe(ev);
  });

  it('does not touch non-role-revealing events', () => {
    const ev = event({ event_type: 'PublicMessageSubmitted', payload: { text: 'hi' } });
    expect(projectEventForPublic(ev, false)).toBe(ev);
  });

  it('catalogs the role-revealing event-type set', () => {
    expect(ROLE_REVEAL_EVENT_TYPES.has('RolesAssigned')).toBe(true);
    expect(ROLE_REVEAL_EVENT_TYPES.has('PlayerEliminated')).toBe(true);
    expect(ROLE_REVEAL_EVENT_TYPES.has('PublicMessageSubmitted')).toBe(false);
  });
});
