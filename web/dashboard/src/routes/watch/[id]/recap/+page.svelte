<script lang="ts">
  import { page } from '$app/stores';
  import { onMount } from 'svelte';
  import Card from '$lib/components/Card.svelte';
  import { padrino } from '$lib/clientStore.svelte';
  import type { PublicGameAnalyticsResponse } from '$lib/api/types';

  let gameId = $derived($page.params.id ?? '');

  let analytics = $state<PublicGameAnalyticsResponse | null>(null);
  let loading = $state(false);
  let notFound = $state(false);
  let error = $state<string | null>(null);

  async function load() {
    loading = true;
    error = null;
    notFound = false;
    try {
      analytics = await padrino.client.publicGameAnalytics(gameId);
    } catch (e) {
      const err = e as { status?: number; message?: string };
      if (err.status === 404) {
        notFound = true;
      } else {
        error = err.message ?? 'Failed to load analytics.';
      }
    }
    loading = false;
  }

  onMount(() => {
    void load();
  });

  const isLive = $derived(analytics !== null && analytics.winner === null);

  const pct = (rate: number) => `${(rate * 100).toFixed(1)}%`;
</script>

<div class="mb-4 flex gap-4">
  <a class="text-sm underline" href="/watch/{gameId}">← Watch</a>
  <a class="text-sm underline" href="/">← Home</a>
</div>

<h1 class="mb-4 text-xl font-semibold" data-testid="recap-title">Post-Match Recap</h1>
<p class="mb-6 font-mono text-xs text-muted-foreground" data-testid="recap-game-id">{gameId}</p>

{#if loading}
  <p class="text-sm text-muted-foreground" data-testid="recap-loading">Loading analytics…</p>
{:else if notFound}
  <p class="text-sm text-muted-foreground" data-testid="recap-not-found">
    Game not found or not yet public.
  </p>
{:else if error}
  <p class="text-sm text-red-500" data-testid="recap-error">{error}</p>
{:else if analytics === null}
  <p class="text-sm text-muted-foreground" data-testid="recap-loading">Loading analytics…</p>
{:else if isLive}
  <div
    class="rounded-md border border-amber-300 bg-amber-50 px-4 py-3 text-sm"
    data-testid="recap-live-notice"
  >
    Recap is available only after the game concludes. Check back once the broadcast ends.
  </div>
{:else}
  <div class="mb-4 flex items-center gap-3" data-testid="recap-outcome">
    <span class="text-sm font-medium text-muted-foreground">Winner:</span>
    <span class="font-semibold" data-testid="recap-winner">{analytics.winner}</span>
  </div>

  <div class="grid gap-4 sm:grid-cols-2">
    <Card>
      <h2 class="mb-3 text-sm font-semibold" data-testid="recap-voting-heading">Voting Accuracy</h2>
      <div class="flex flex-col gap-1 text-sm" data-testid="recap-voting-accuracy">
        <div class="flex justify-between">
          <span class="text-muted-foreground">Total votes</span>
          <span data-testid="recap-total-votes">{analytics.voting_accuracy.total_votes}</span>
        </div>
        <div class="flex justify-between">
          <span class="text-muted-foreground">Accurate (hit Mafia)</span>
          <span data-testid="recap-accurate-votes"
            >{analytics.voting_accuracy.accurate_votes}</span
          >
        </div>
        <div class="flex justify-between font-semibold">
          <span>Accuracy rate</span>
          <span data-testid="recap-vote-rate">{pct(analytics.voting_accuracy.rate)}</span>
        </div>
      </div>
    </Card>

    {#if analytics.role_win_rates !== null && analytics.role_win_rates.length > 0}
      <Card>
        <h2 class="mb-3 text-sm font-semibold" data-testid="recap-winrates-heading">
          Role Win Rates
        </h2>
        <ul class="flex flex-col gap-1" data-testid="recap-role-win-rates">
          {#each analytics.role_win_rates as rwr (rwr.role)}
            <li
              class="flex items-center justify-between text-sm"
              data-testid="recap-role-win-rate-row"
            >
              <span class="font-mono text-xs">{rwr.role}</span>
              <span>{rwr.wins}/{rwr.games} ({pct(rwr.rate)})</span>
            </li>
          {/each}
        </ul>
      </Card>
    {/if}
  </div>

  {#if analytics.survival_curve.length > 0}
    <Card class="mt-4">
      <h2 class="mb-3 text-sm font-semibold" data-testid="recap-survival-heading">
        Survival by Role
      </h2>
      <ul class="flex flex-col gap-1" data-testid="recap-survival-curve">
        {#each analytics.survival_curve as sp (`${sp.role}-${sp.day}`)}
          <li class="flex items-center justify-between text-xs" data-testid="recap-survival-row">
            <span class="font-mono">{sp.role} Day {sp.day}</span>
            <span>{sp.alive_count}/{sp.total_count} ({pct(sp.fraction)})</span>
          </li>
        {/each}
      </ul>
    </Card>
  {/if}

  {#if analytics.claims.length > 0}
    <Card class="mt-4">
      <h2 class="mb-3 text-sm font-semibold" data-testid="recap-claims-heading">Role Claims</h2>
      <ul class="flex flex-col gap-1" data-testid="recap-claims">
        {#each analytics.claims as claim (claim.sequence)}
          <li class="text-xs" data-testid="recap-claim-row">
            <span class="font-mono">{claim.player_id.slice(0, 8)}</span>
            claimed
            <span class="font-semibold">{claim.claimed_role}</span>
            <span class="text-muted-foreground">({claim.phase})</span>
          </li>
        {/each}
      </ul>
      {#if analytics.counter_claims.length > 0}
        <div class="mt-3" data-testid="recap-counter-claims">
          <p class="mb-1 text-xs font-semibold text-amber-700">Counter-claims</p>
          <ul class="flex flex-col gap-1">
            {#each analytics.counter_claims as ccg (ccg.claimed_role)}
              <li class="text-xs" data-testid="recap-counter-claim-row">
                <span class="font-semibold">{ccg.claimed_role}</span>: {ccg.claimants.length} claimants
              </li>
            {/each}
          </ul>
        </div>
      {/if}
    </Card>
  {/if}
{/if}
