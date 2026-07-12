import { describe, expect, it } from 'vitest';
import {
  factionBadgeTone,
  modelTierBadgeTone,
  roleBadgeTone,
  safeSeatTone,
  statusBadgeTone
} from './tokens';

describe('design token contracts', () => {
  it('uses neutral presence-only styling before endgame reveal', () => {
    expect(safeSeatTone({ alive: true })).toEqual({ className: 'seat-alive', label: 'Active' });
    expect(safeSeatTone({ alive: false })).toEqual({ className: 'seat-eliminated', label: 'Eliminated' });
  });

  it('keeps hidden identity and role facts out of the pre-reveal token input', () => {
    expect(Object.keys(safeSeatTone({ alive: true }))).toEqual(['className', 'label']);
  });

  it('provides semantic status, model-tier, role, and faction badge tokens', () => {
    expect(statusBadgeTone('live')).toBe('status-live');
    expect(modelTierBadgeTone('ranked-qualified')).toBe('model-ranked');
    expect(roleBadgeTone('DETECTIVE')).toBe('role-detective');
    expect(factionBadgeTone('MAFIA')).toBe('faction-mafia');
  });

  it('fails closed to the neutral token for unknown visual facts', () => {
    expect(statusBadgeTone('unexpected')).toBe('status-neutral');
    expect(modelTierBadgeTone('unexpected')).toBe('model-neutral');
    expect(roleBadgeTone('unexpected')).toBe('role-neutral');
    expect(factionBadgeTone('unexpected')).toBe('faction-neutral');
  });
});
