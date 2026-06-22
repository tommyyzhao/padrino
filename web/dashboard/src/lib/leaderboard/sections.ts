import type { PublicRatingCardResponse } from '$lib/api/types';

export interface LeaderboardCardSections {
  canonicalGlobalCards: PublicRatingCardResponse[];
  canonicalFactionCards: PublicRatingCardResponse[];
  canonicalOtherCards: PublicRatingCardResponse[];
  humanCards: PublicRatingCardResponse[];
  placementCards: PublicRatingCardResponse[];
  soloRateCards: PublicRatingCardResponse[];
  experimentalOtherCards: PublicRatingCardResponse[];
}

export function splitLeaderboardCards(
  canonicalCards: PublicRatingCardResponse[],
  experimentalCards: PublicRatingCardResponse[],
  humanCards: PublicRatingCardResponse[] = []
): LeaderboardCardSections {
  return {
    canonicalGlobalCards: canonicalCards.filter((card) => card.scope_type === 'GLOBAL'),
    canonicalFactionCards: canonicalCards.filter((card) => card.scope_type === 'FACTION'),
    canonicalOtherCards: canonicalCards.filter(
      (card) => card.scope_type !== 'GLOBAL' && card.scope_type !== 'FACTION'
    ),
    humanCards,
    placementCards: experimentalCards.filter((card) => card.context_kind === 'PLACEMENT'),
    soloRateCards: experimentalCards.filter((card) => card.context_kind === 'SOLO_RATE'),
    experimentalOtherCards: experimentalCards.filter(
      (card) => card.context_kind !== 'PLACEMENT' && card.context_kind !== 'SOLO_RATE'
    )
  };
}
