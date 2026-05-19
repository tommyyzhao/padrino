import type { PublicEventEntry } from '$lib/api/types';

// Public projection: role/faction stay hidden until the game is terminal.
// The role-revealing event types are tagged here so the UI can collapse them
// into a single "(role reveal hidden)" placeholder while the game is live.
export const ROLE_REVEAL_EVENT_TYPES = new Set([
  'RolesAssigned',
  'PlayerEliminated',
  'GameTerminated'
]);

export interface PhaseGroup {
  phase: string;
  index: number;
  events: PublicEventEntry[];
}

export function groupEventsByPhase(events: readonly PublicEventEntry[]): PhaseGroup[] {
  const groups: PhaseGroup[] = [];
  const indexByPhase = new Map<string, number>();
  for (const event of events) {
    const phase = event.phase || 'UNKNOWN';
    let idx = indexByPhase.get(phase);
    if (idx === undefined) {
      idx = groups.length;
      indexByPhase.set(phase, idx);
      groups.push({ phase, index: idx, events: [] });
    }
    groups[idx].events.push(event);
  }
  return groups;
}

export interface ScrubberState {
  groups: PhaseGroup[];
  currentIndex: number;
}

export function initScrubber(events: readonly PublicEventEntry[]): ScrubberState {
  const groups = groupEventsByPhase(events);
  return { groups, currentIndex: groups.length > 0 ? 0 : -1 };
}

export function next(state: ScrubberState): ScrubberState {
  if (state.groups.length === 0) return state;
  const idx = Math.min(state.groups.length - 1, state.currentIndex + 1);
  return { ...state, currentIndex: idx };
}

export function prev(state: ScrubberState): ScrubberState {
  if (state.groups.length === 0) return state;
  const idx = Math.max(0, state.currentIndex - 1);
  return { ...state, currentIndex: idx };
}

export function jumpTo(state: ScrubberState, target: number | string): ScrubberState {
  if (state.groups.length === 0) return state;
  let idx: number;
  if (typeof target === 'number') {
    idx = target;
  } else {
    const found = state.groups.findIndex((g) => g.phase === target);
    idx = found === -1 ? state.currentIndex : found;
  }
  const clamped = Math.max(0, Math.min(state.groups.length - 1, idx));
  return { ...state, currentIndex: clamped };
}

export function currentGroup(state: ScrubberState): PhaseGroup | null {
  if (state.currentIndex < 0 || state.currentIndex >= state.groups.length) return null;
  return state.groups[state.currentIndex];
}

// Role-safe projection: when the game is not yet terminal, swallow the
// payload of role-revealing events so the dashboard never leaks identity.
export function projectEventForPublic(
  event: PublicEventEntry,
  isTerminal: boolean
): PublicEventEntry {
  if (isTerminal) return event;
  if (!ROLE_REVEAL_EVENT_TYPES.has(event.event_type)) return event;
  return {
    ...event,
    payload: { redacted: true, reason: 'role_hidden_until_terminal' }
  };
}
