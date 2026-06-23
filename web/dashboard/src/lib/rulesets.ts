import type { PublicRulesetEntry } from './api/types';

export function canonicalTeamRulesets(items: PublicRulesetEntry[]): PublicRulesetEntry[] {
  return items.filter(
    (ruleset) => ruleset.is_canonical && ruleset.rating_context_kind === 'CANONICAL_TEAM'
  );
}
