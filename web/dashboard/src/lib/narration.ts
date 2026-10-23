// US-411: pure helpers for the narrated spectator queue. Public frame fields only.

export interface NarratedFrame {
  sequence: number;
  event_type: string;
  phase: string;
  actor_player_id: string | null;
  payload: Record<string, unknown>;
}

export interface SpeechItem {
  /** Full public seat id (null = narrator). */
  speaker: string | null;
  text: string;
  cue: 'chat' | 'phase';
  sequence: number;
}

const short = (id: string): string => id.slice(0, 8);
const str = (v: unknown): string | null => (typeof v === 'string' ? v : null);

/** Caption dwell when audio is unavailable, muted-timing aside. */
export function dwellMs(text: string): number {
  return Math.min(8000, 1200 + 45 * text.length);
}

export function phaseLabel(phase: string): string {
  const s = phase.replace(/_/g, ' ').trim().toLowerCase();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** What this frame says aloud, or null when it is silent. */
export function speechFor(frame: NarratedFrame): SpeechItem | null {
  const { sequence, payload } = frame;
  switch (frame.event_type) {
    case 'PublicMessageSubmitted': {
      const text = str(payload['text']);
      if (frame.actor_player_id === null || text === null) return null;
      return { speaker: frame.actor_player_id, text, cue: 'chat', sequence };
    }
    case 'PlayerEliminated': {
      const pid = str(payload['public_player_id']) ?? frame.actor_player_id;
      if (pid === null) return null;
      return { speaker: null, text: `${short(pid)} was eliminated.`, cue: 'chat', sequence };
    }
    case 'GameTerminated': {
      const winner = str(payload['winner']);
      const text = winner ? `Game over. Winner: ${winner}.` : 'Game over.';
      return { speaker: null, text, cue: 'chat', sequence };
    }
    default:
      return null;
  }
}

/** Phase announcement when `frame.phase` differs from the previously presented phase. */
export function phaseCueFor(frame: NarratedFrame, prevPhase: string | null): SpeechItem | null {
  if (frame.phase === prevPhase) return null;
  return { speaker: null, text: phaseLabel(frame.phase), cue: 'phase', sequence: frame.sequence };
}
