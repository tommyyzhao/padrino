import type { PublicRulesetEntry } from './api/types';

export const CANONICAL_TEAM_RULESET_FALLBACK_IDS = ['mini7_v1', 'bench10_v1'] as const;

export function canonicalTeamRulesets(items: PublicRulesetEntry[]): PublicRulesetEntry[] {
  return items.filter(
    (ruleset) => ruleset.is_canonical && ruleset.rating_context_kind === 'CANONICAL_TEAM'
  );
}
