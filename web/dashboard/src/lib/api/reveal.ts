// US-156: Endgame reveal helpers.
//
// At the reveal, every seat's full truth is disclosed (role/faction/human-AI),
// so a themed sprite may be keyed on the seat's role archetype (anonymity no
// longer applies post-reveal, AGENTS.md rule 7). This resolves a stable,
// role-derived sprite KEY; the immutable asset URL is assembled by
// `spriteUrl` in `playSurface.ts`.

import type { SeatReveal } from './types';

/**
 * Resolve a deterministic themed-sprite key for a revealed seat.
 *
 * Post-reveal the seat's role is public, so the sprite reflects the role
 * archetype; this is purely cosmetic and never affects mechanics. A missing
 * role falls back to the deterministic placeholder.
 */
export function revealSpriteKey(seat: SeatReveal): string {
  const role = (seat.role ?? '').trim().toLowerCase();
  if (role === '') return 'placeholder';
  return role;
}
