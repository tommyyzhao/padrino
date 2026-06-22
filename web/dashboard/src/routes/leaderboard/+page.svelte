<script lang="ts">
  import { onMount } from 'svelte';
  import { padrino } from '$lib/clientStore.svelte';
  import type { PublicRatingCardResponse } from '$lib/api/types';
  import LeaderboardSections from '$lib/leaderboard/LeaderboardSections.svelte';

  let canonicalCards = $state<PublicRatingCardResponse[]>([]);
  let experimentalCards = $state<PublicRatingCardResponse[]>([]);
  let humanCards = $state<PublicRatingCardResponse[]>([]);
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
      humanCards = response.human_cards;
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  }

  onMount(load);
</script>

<h1 class="mb-4 text-2xl font-semibold" data-testid="leaderboard-title">Leaderboard</h1>

<div class="space-y-8">
  {#if loading}
    <p data-testid="leaderboard-loading">Loading…</p>
  {:else if error}
    <p class="text-sm text-red-500" data-testid="leaderboard-error">{error}</p>
  {:else if canonicalCards.length === 0 && experimentalCards.length === 0 && humanCards.length === 0}
    <p class="text-sm text-muted-foreground" data-testid="leaderboard-empty">No entries yet.</p>
  {:else}
    <LeaderboardSections {canonicalCards} {experimentalCards} {humanCards} />
  {/if}
</div>
