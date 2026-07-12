/**
 * Safe visual-token selection for the dashboard.
 *
 * Identity-blind game surfaces receive only `safeSeatTone`: it accepts no role,
 * faction, model, or controller information. Role and faction tokens exist for
 * the terminal reveal and other already-authorized retrospective surfaces.
 */

export interface SafeSeatFacts {
  alive: boolean;
}

export interface SafeSeatTone {
  className: 'seat-alive' | 'seat-eliminated';
  label: 'Active' | 'Eliminated';
}

const STATUS_TONES: Record<string, string> = {
  live: 'status-live',
  ready: 'status-ready',
  waiting: 'status-waiting',
  warning: 'status-warning',
  error: 'status-error',
  ended: 'status-ended'
};

const MODEL_TIER_TONES: Record<string, string> = {
  unverified: 'model-unverified',
  playable: 'model-playable',
  'verified-structured': 'model-verified',
  'ranked-qualified': 'model-ranked',
  provisional: 'model-provisional',
  established: 'model-established'
};

const ROLE_TONES: Record<string, string> = {
  VILLAGER: 'role-villager',
  MAFIA: 'role-mafia',
  DETECTIVE: 'role-detective',
  DOCTOR: 'role-doctor',
  ROLEBLOCKER: 'role-roleblocker',
  TRACKER: 'role-tracker',
  WATCHER: 'role-watcher',
  NINJA: 'role-ninja',
  JANITOR: 'role-janitor',
  SERIAL_KILLER: 'role-serial-killer',
  JESTER: 'role-jester'
};

const FACTION_TONES: Record<string, string> = {
  TOWN: 'faction-town',
  MAFIA: 'faction-mafia',
  NEUTRAL: 'faction-neutral',
  SERIAL_KILLER: 'faction-serial-killer',
  JESTER: 'faction-jester'
};

export function safeSeatTone(facts: SafeSeatFacts): SafeSeatTone {
  return facts.alive
    ? { className: 'seat-alive', label: 'Active' }
    : { className: 'seat-eliminated', label: 'Eliminated' };
}

export function statusBadgeTone(status: string): string {
  return STATUS_TONES[status.toLowerCase()] ?? 'status-neutral';
}

export function modelTierBadgeTone(tier: string): string {
  return MODEL_TIER_TONES[tier.toLowerCase()] ?? 'model-neutral';
}

export function roleBadgeTone(role: string): string {
  return ROLE_TONES[role.toUpperCase()] ?? 'role-neutral';
}

export function factionBadgeTone(faction: string): string {
  return FACTION_TONES[faction.toUpperCase()] ?? 'faction-neutral';
}
