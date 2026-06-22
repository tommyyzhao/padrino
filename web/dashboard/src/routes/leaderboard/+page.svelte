<script lang="ts">
  import { onMount } from 'svelte';
  import Card from '$lib/components/Card.svelte';
  import { padrino } from '$lib/clientStore.svelte';
  import type { PublicRatingCardResponse } from '$lib/api/types';

  let canonicalCards = $state<PublicRatingCardResponse[]>([]);
  let experimentalCards = $state<PublicRatingCardResponse[]>([]);
  let loading = $state(false);
  let error = $state<string | null>(null);

  async function load() {
    loading = true;
    error = null;
    try {
      const response = await padrino.client.publicLeaderboard({
        limit: 100
      });
      canonicalCards = response.canonical_cards;
      experimentalCards = response.experimental_cards;
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  }

  function scoreText(card: PublicRatingCardResponse): string {
    if (card.metric === 'solo_success_rate') {
      const value = card.mean_success_rate ?? card.score;
      return `${(value * 100).toFixed(0)}%`;
    }
    return (card.conservative_score ?? card.score).toFixed(1);
  }

  function uncertaintyText(card: PublicRatingCardResponse): string {
    if (
      card.metric === 'solo_success_rate' &&
      card.credible_interval_low !== null &&
      card.credible_interval_high !== null
    ) {
      return `CI ${(card.credible_interval_low * 100).toFixed(0)}-${(card.credible_interval_high * 100).toFixed(0)}%`;
    }
    if (card.sigma !== null) {
      return `±${card.sigma.toFixed(1)}`;
    }
    return '';
  }

  function rankText(card: PublicRatingCardResponse): string {
    if (card.provisional) return 'Provisional';
    if (card.rank !== null) return `#${card.rank}`;
    return 'Unranked';
  }

  function sampleText(card: PublicRatingCardResponse): string {
    if (card.metric === 'solo_success_rate') {
      return `${card.successes ?? 0}/${card.attempts ?? 0}`;
    }
    return `${card.games ?? 0}`;
  }

  onMount(load);
</script>

<h1 class="mb-4 text-2xl font-semibold" data-testid="leaderboard-title">Leaderboard</h1>

<div class="space-y-8">
  {#if loading}
    <p data-testid="leaderboard-loading">Loading…</p>
  {:else if error}
    <p class="text-sm text-red-500" data-testid="leaderboard-error">{error}</p>
  {:else if canonicalCards.length === 0 && experimentalCards.length === 0}
    <p class="text-sm text-muted-foreground" data-testid="leaderboard-empty">No entries yet.</p>
  {:else}
    <section class="space-y-3" data-testid="leaderboard-canonical-section">
      <div class="flex items-center justify-between gap-3">
        <h2 class="text-lg font-semibold">Ranked Canonical</h2>
        <span class="text-xs uppercase tracking-wider text-muted-foreground">Canonical</span>
      </div>
      {#if canonicalCards.length === 0}
        <p class="text-sm text-muted-foreground" data-testid="leaderboard-canonical-empty">
          No canonical cards yet.
        </p>
      {:else}
        <div class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {#each canonicalCards as card (card.card_id)}
            <Card
              class="space-y-4"
              data-testid="leaderboard-card"
              data-section={card.section}
              data-context={card.context_kind}
            >
              <div class="flex items-start justify-between gap-3">
                <div class="min-w-0">
                  <p class="text-xs uppercase tracking-wider text-muted-foreground">
                    {card.context_label}
                  </p>
                  <h3 class="truncate text-base font-semibold" data-testid="leaderboard-card-name">
                    {card.display_name}
                  </h3>
                </div>
                <span class="shrink-0 text-sm font-medium" data-testid="leaderboard-card-rank">
                  {rankText(card)}
                </span>
              </div>
              <div class="flex items-end justify-between gap-3">
                <div>
                  <p class="text-xs text-muted-foreground">{card.metric_label}</p>
                  <p class="text-3xl font-semibold">{scoreText(card)}</p>
                </div>
                <p class="text-sm text-muted-foreground">{uncertaintyText(card)}</p>
              </div>
              <div class="flex items-center justify-between text-sm">
                <span class="text-muted-foreground">Games</span>
                <span>{sampleText(card)}</span>
              </div>
              {#if card.provisional_reason}
                <p class="text-xs text-muted-foreground" data-testid="leaderboard-card-provisional">
                  {card.provisional_reason}
                </p>
              {/if}
            </Card>
          {/each}
        </div>
      {/if}
    </section>

    <section class="space-y-3" data-testid="leaderboard-experimental-section">
      <div class="flex items-center justify-between gap-3">
        <h2 class="text-lg font-semibold">Experimental Contexts</h2>
        <span class="text-xs uppercase tracking-wider text-muted-foreground">Non-canonical</span>
      </div>
      {#if experimentalCards.length === 0}
        <p class="text-sm text-muted-foreground" data-testid="leaderboard-experimental-empty">
          No experimental cards yet.
        </p>
      {:else}
        <div class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {#each experimentalCards as card (card.card_id)}
            <Card
              class="space-y-4"
              data-testid="leaderboard-card"
              data-section={card.section}
              data-context={card.context_kind}
            >
              <div class="flex items-start justify-between gap-3">
                <div class="min-w-0">
                  <p class="text-xs uppercase tracking-wider text-muted-foreground">
                    {card.context_label}
                  </p>
                  <h3 class="truncate text-base font-semibold" data-testid="leaderboard-card-name">
                    {card.display_name}
                  </h3>
                </div>
                <span class="shrink-0 text-sm font-medium" data-testid="leaderboard-card-rank">
                  {rankText(card)}
                </span>
              </div>
              <div class="flex items-end justify-between gap-3">
                <div>
                  <p class="text-xs text-muted-foreground">{card.metric_label}</p>
                  <p class="text-3xl font-semibold">{scoreText(card)}</p>
                </div>
                <p class="text-sm text-muted-foreground">{uncertaintyText(card)}</p>
              </div>
              <div class="flex items-center justify-between text-sm">
                <span class="text-muted-foreground">
                  {card.metric === 'solo_success_rate' ? 'Successes' : 'Games'}
                </span>
                <span>{sampleText(card)}</span>
              </div>
              {#if card.provisional_reason}
                <p class="text-xs text-muted-foreground" data-testid="leaderboard-card-provisional">
                  {card.provisional_reason}
                </p>
              {/if}
            </Card>
          {/each}
        </div>
      {/if}
    </section>
  {/if}
</div>
