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

const str = (v: unknown): string | null => (typeof v === 'string' ? v : null);
const PHASE_RE = /^(DAY|NIGHT)_(\d+)/;

/** Caption dwell when audio is unavailable, muted-timing aside. */
export function dwellMs(text: string): number {
  return Math.min(8000, 1200 + 45 * text.length);
}

/** Announcement unit: `DAY_1`, `DAY_1_VOTE`, `NIGHT_1`; discussion rounds collapse. */
function phaseSegment(phase: string): string {
  const m = PHASE_RE.exec(phase);
  if (!m) return phase;
  return m[1] === 'DAY' && phase.includes('VOTE') ? `${m[0]}_VOTE` : m[0];
}

/** Mirrors `phase_text` in `api/routes/speech.py` so captions match the audio. */
export function phaseLabel(phase: string): string {
  const m = PHASE_RE.exec(phase);
  if (!m) return 'A new phase begins.';
  if (m[1] === 'NIGHT') return `Night ${m[2]} falls.`;
  return phase.includes('VOTE') ? `Day ${m[2]}, voting begins.` : `Day ${m[2]} begins.`;
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
      return { speaker: null, text: `${pid} has been eliminated.`, cue: 'chat', sequence };
    }
    case 'GameTerminated': {
      const winner = str(payload['winner']) ?? '';
      return { speaker: null, text: `The game is over. ${winner} wins.`, cue: 'chat', sequence };
    }
    default:
      return null;
  }
}

/** Phase announcement when the frame enters a new day/vote/night segment. */
export function phaseCueFor(frame: NarratedFrame, prevPhase: string | null): SpeechItem | null {
  if (!PHASE_RE.test(frame.phase)) return null; // setup/terminal: the result line speaks
  if (prevPhase !== null && phaseSegment(frame.phase) === phaseSegment(prevPhase)) return null;
  return { speaker: null, text: phaseLabel(frame.phase), cue: 'phase', sequence: frame.sequence };
}
