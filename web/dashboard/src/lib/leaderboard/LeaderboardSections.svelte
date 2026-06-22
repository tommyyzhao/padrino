<script lang="ts">
  import type { PublicRatingCardResponse } from '$lib/api/types';
  import LeaderboardCard from './LeaderboardCard.svelte';
  import { splitLeaderboardCards } from './sections';

  interface Props {
    canonicalCards: PublicRatingCardResponse[];
    experimentalCards: PublicRatingCardResponse[];
  }

  let { canonicalCards, experimentalCards }: Props = $props();

  let sections = $derived(splitLeaderboardCards(canonicalCards, experimentalCards));
</script>

<section class="space-y-5" data-testid="leaderboard-canonical-section">
  <div class="flex items-center justify-between gap-3">
    <h2 class="text-lg font-semibold">Ranked Canonical</h2>
    <span class="text-xs uppercase tracking-wider text-muted-foreground">Canonical</span>
  </div>
  {#if canonicalCards.length === 0}
    <p class="text-sm text-muted-foreground" data-testid="leaderboard-canonical-empty">
      No canonical cards yet.
    </p>
  {:else}
    <div class="space-y-6">
      <section
        class="space-y-3 border-l-2 border-border pl-4"
        data-testid="leaderboard-canonical-global-subsection"
      >
        <div class="flex items-center justify-between gap-3">
          <h3 class="text-sm font-semibold">Global Canonical</h3>
          <span class="text-xs uppercase tracking-wider text-muted-foreground">GLOBAL</span>
        </div>
        {#if sections.canonicalGlobalCards.length === 0}
          <p class="text-sm text-muted-foreground" data-testid="leaderboard-canonical-global-empty">
            No global canonical cards yet.
          </p>
        {:else}
          <div
            class="grid gap-3 md:grid-cols-2 xl:grid-cols-3"
            data-testid="leaderboard-canonical-global-grid"
          >
            {#each sections.canonicalGlobalCards as card (card.card_id)}
              <LeaderboardCard card={card} subsection="canonical-global" />
            {/each}
          </div>
        {/if}
      </section>

      <section
        class="space-y-3 border-l-2 border-border pl-4"
        data-testid="leaderboard-canonical-faction-subsection"
      >
        <div class="flex items-center justify-between gap-3">
          <h3 class="text-sm font-semibold">Faction Cards</h3>
          <span class="text-xs uppercase tracking-wider text-muted-foreground">FACTION</span>
        </div>
        {#if sections.canonicalFactionCards.length === 0}
          <p class="text-sm text-muted-foreground" data-testid="leaderboard-canonical-faction-empty">
            No faction cards yet.
          </p>
        {:else}
          <div
            class="grid gap-3 md:grid-cols-2 xl:grid-cols-3"
            data-testid="leaderboard-canonical-faction-grid"
          >
            {#each sections.canonicalFactionCards as card (card.card_id)}
              <LeaderboardCard card={card} subsection="canonical-faction" />
            {/each}
          </div>
        {/if}
      </section>

      {#if sections.canonicalOtherCards.length > 0}
        <section
          class="space-y-3 border-l-2 border-border pl-4"
          data-testid="leaderboard-canonical-other-subsection"
        >
          <div class="flex items-center justify-between gap-3">
            <h3 class="text-sm font-semibold">Other Canonical Scopes</h3>
            <span class="text-xs uppercase tracking-wider text-muted-foreground">Scoped</span>
          </div>
          <div class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {#each sections.canonicalOtherCards as card (card.card_id)}
              <LeaderboardCard card={card} subsection="canonical-other" />
            {/each}
          </div>
        </section>
      {/if}
    </div>
  {/if}
</section>

<div class="space-y-8" data-testid="leaderboard-experimental-section">
  <section class="space-y-3" data-testid="leaderboard-placement-section">
    <div class="flex items-center justify-between gap-3">
      <h2 class="text-lg font-semibold">Placement Contexts</h2>
      <span class="text-xs uppercase tracking-wider text-muted-foreground">PLACEMENT</span>
    </div>
    {#if sections.placementCards.length === 0}
      <p class="text-sm text-muted-foreground" data-testid="leaderboard-placement-empty">
        No placement cards yet.
      </p>
    {:else}
      <div class="grid gap-3 md:grid-cols-2 xl:grid-cols-3" data-testid="leaderboard-placement-grid">
        {#each sections.placementCards as card (card.card_id)}
          <LeaderboardCard card={card} subsection="placement" />
        {/each}
      </div>
    {/if}
  </section>

  <section class="space-y-3" data-testid="leaderboard-solo-rate-section">
    <div class="flex items-center justify-between gap-3">
      <h2 class="text-lg font-semibold">Solo Rate Contexts</h2>
      <span class="text-xs uppercase tracking-wider text-muted-foreground">SOLO_RATE</span>
    </div>
    {#if sections.soloRateCards.length === 0}
      <p class="text-sm text-muted-foreground" data-testid="leaderboard-solo-rate-empty">
        No solo rate cards yet.
      </p>
    {:else}
      <div class="grid gap-3 md:grid-cols-2 xl:grid-cols-3" data-testid="leaderboard-solo-rate-grid">
        {#each sections.soloRateCards as card (card.card_id)}
          <LeaderboardCard card={card} subsection="solo-rate" />
        {/each}
      </div>
    {/if}
  </section>

  {#if sections.experimentalOtherCards.length > 0}
    <section class="space-y-3" data-testid="leaderboard-experimental-other-section">
      <div class="flex items-center justify-between gap-3">
        <h2 class="text-lg font-semibold">Other Experimental Contexts</h2>
        <span class="text-xs uppercase tracking-wider text-muted-foreground">Experimental</span>
      </div>
      <div class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {#each sections.experimentalOtherCards as card (card.card_id)}
          <LeaderboardCard card={card} subsection="experimental-other" />
        {/each}
      </div>
    </section>
  {/if}
</div>
