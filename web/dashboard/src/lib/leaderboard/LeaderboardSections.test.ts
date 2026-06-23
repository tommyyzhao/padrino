import { describe, expect, it } from 'vitest';
import type { PublicRatingCardResponse } from '$lib/api/types';
import { splitLeaderboardCards } from './sections';

function ratingCard(overrides: Partial<PublicRatingCardResponse>): PublicRatingCardResponse {
  return {
    card_id: 'card-default',
    section: 'canonical',
    section_label: 'Ranked canonical',
    context_kind: 'CANONICAL_TEAM',
    context_label: 'Bench 10 canonical team',
    ruleset_id: 'bench10_v1',
    entity_id: 'entity-default',
    display_name: 'Default',
    model_provider: 'mock',
    model_name: 'mock-model',
    model_version: null,
    prompt_version: 'v1',
    scope_type: 'GLOBAL',
    scope_value: 'global',
    metric: 'openskill_conservative',
    metric_label: 'Canonical ELO',
    score: 31.2,
    rank: 1,
    provisional: false,
    provisional_reason: null,
    sample_count: 12,
    games: 12,
    attempts: null,
    successes: null,
    mu: 37.5,
    sigma: 2.1,
    conservative_score: 31.2,
    mean_success_rate: null,
    credible_interval_low: null,
    credible_interval_high: null,
    ...overrides
  };
}

describe('LeaderboardSections', () => {
  it('keeps FACTION-scope canonical cards outside the GLOBAL canonical subsection', () => {
    const global = ratingCard({
      card_id: 'card-global',
      display_name: 'Town Named Global Agent',
      scope_type: 'GLOBAL',
      scope_value: 'global'
    });
    const town = ratingCard({
      card_id: 'card-town',
      display_name: 'Global Named Town Agent',
      scope_type: 'FACTION',
      scope_value: 'TOWN'
    });
    const scum = ratingCard({
      card_id: 'card-scum',
      display_name: 'Scum Agent',
      scope_type: 'FACTION',
      scope_value: 'MAFIA'
    });

    const sections = splitLeaderboardCards([global], [town, scum], []);

    expect(sections.canonicalGlobalCards.map((card) => card.card_id)).toEqual(['card-global']);
    expect(sections.canonicalFactionCards.map((card) => card.card_id)).toEqual([
      'card-town',
      'card-scum'
    ]);
  });

  it('keeps Humans-Included cards in their own section input', () => {
    const canonical = ratingCard({
      card_id: 'card-canonical',
      section: 'canonical',
      context_kind: 'CANONICAL_TEAM'
    });
    const experimental = ratingCard({
      card_id: 'card-placement',
      section: 'experimental',
      context_kind: 'PLACEMENT'
    });
    const human = ratingCard({
      card_id: 'card-human',
      section: 'humans_included',
      section_label: 'Humans-Included League',
      context_kind: 'HUMANS_INCLUDED',
      context_label: 'Humans-Included mini7_v1 ranked',
      display_name: 'Human Ace',
      model_provider: 'human',
      model_name: 'human_player',
      prompt_version: 'humans-included',
      metric_label: 'Human ELO'
    });

    const sections = splitLeaderboardCards([canonical], [], [experimental], [human]);

    expect(sections.canonicalGlobalCards.map((card) => card.card_id)).toEqual([
      'card-canonical'
    ]);
    expect(sections.placementCards.map((card) => card.card_id)).toEqual(['card-placement']);
    expect(sections.humanCards.map((card) => card.card_id)).toEqual(['card-human']);
  });
});
