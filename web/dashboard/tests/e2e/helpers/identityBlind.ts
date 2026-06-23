import { expect, type Locator } from '@playwright/test';

export const IDENTITY_MARKER_TOKENS = [
  'seat_kind',
  'is_human',
  'agent_build',
  'agent_build_id',
  'controller_type',
  'takeover',
  'occupant_principal_id',
  'human_player_id',
  'model_provider',
  'model_name',
  'provider_name'
] as const;

export const ROLE_AND_FACTION_TOKENS = [
  'mafia',
  'town',
  'mafia_goon',
  'mafioso',
  'villager',
  'doctor',
  'detective',
  'roleblocker',
  'tracker',
  'watcher',
  'ninja',
  'janitor',
  'serial_killer',
  'serial killer',
  'jester'
] as const;

export async function expectIdentityBlind(
  surface: Locator,
  extraForbiddenTokens: readonly string[] = []
): Promise<void> {
  const html = (await surface.innerHTML()).toLowerCase();
  for (const token of [...IDENTITY_MARKER_TOKENS, ...extraForbiddenTokens]) {
    expect(html, `identity-blind surface leaked ${token}`).not.toContain(token.toLowerCase());
  }
}
