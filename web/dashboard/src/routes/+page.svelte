<script lang="ts">
  import { onMount } from 'svelte';
  import Card from '$lib/components/Card.svelte';
  import { padrino } from '$lib/clientStore.svelte';
  import type { PublicModelEntryResponse } from '$lib/api/types';

  const RULESET = 'mini7_v1';

  let totalGames = $state<number | null>(null);
  let activeGauntlets = $state<number | null>(null);
  let topModels = $state<PublicModelEntryResponse[]>([]);
  let error = $state<string | null>(null);

  async function load() {
    try {
      // Top models: requires a league id; pick the first available league via
      // the public surface indirectly — for the home KPI we accept a fallback
      // to the global public leaderboard (entity-keyed, identity-blind).
      const lb = await padrino.client.publicLeaderboard({ ruleset_id: RULESET, limit: 3 });
      topModels = lb.entries.slice(0, 3).map((e) => ({
        model_key: `${e.model_provider}/${e.model_name}${e.model_version ? '@' + e.model_version : ''}`,
        display_name: e.display_name,
        model_provider: e.model_provider,
        model_name: e.model_name,
        model_version: e.model_version,
        mu: e.mu,
        sigma: e.sigma,
        conservative_score: e.conservative_score,
        games: e.games,
        wins: e.wins,
        draws: e.draws,
        losses: e.losses,
        town: { mu: 0, sigma: 0, conservative_score: 0, games: 0, wins: 0, draws: 0, losses: 0 },
        mafia: { mu: 0, sigma: 0, conservative_score: 0, games: 0, wins: 0, draws: 0, losses: 0 },
        agent_build_count: 1
      }));
    } catch (e) {
      // Anonymous mode may not be enabled; degrade silently for KPIs.
      error = (e as Error).message;
    }

    try {
      const games = await padrino.client.listGames({ limit: 1 });
      totalGames = games.total_estimate ?? games.items.length;
    } catch {
      totalGames = null;
    }

    try {
      const running = await padrino.client.listGauntlets({ status: 'RUNNING', limit: 50 });
      activeGauntlets = running.items.length;
    } catch {
      activeGauntlets = null;
    }
  }

  onMount(load);
</script>

<div class="grid gap-4 sm:grid-cols-3">
  <Card>
    <div class="text-xs uppercase tracking-wider text-muted-foreground">Total games</div>
    <div class="mt-1 text-3xl font-semibold">{totalGames ?? '—'}</div>
  </Card>
  <Card>
    <div class="text-xs uppercase tracking-wider text-muted-foreground">Active gauntlets</div>
    <div class="mt-1 text-3xl font-semibold">{activeGauntlets ?? '—'}</div>
  </Card>
  <Card>
    <div class="text-xs uppercase tracking-wider text-muted-foreground">Top model</div>
    <div class="mt-1 text-xl font-semibold">
      {topModels[0]?.display_name ?? '—'}
    </div>
  </Card>
</div>

<section class="mt-8">
  <h2 class="mb-3 text-lg font-semibold">Top 3 models</h2>
  {#if topModels.length === 0}
    <p class="text-sm text-muted-foreground">No ranked results yet.</p>
  {:else}
    <Card>
      <table class="w-full text-sm">
        <thead class="text-left text-xs uppercase tracking-wider text-muted-foreground">
          <tr>
            <th class="pb-2">Rank</th>
            <th class="pb-2">Model</th>
            <th class="pb-2 text-right">Score</th>
            <th class="pb-2 text-right">Games</th>
          </tr>
        </thead>
        <tbody>
          {#each topModels as model, i (model.model_key)}
            <tr class="border-t border-border">
              <td class="py-2">{i + 1}</td>
              <td class="py-2 font-medium">{model.display_name}</td>
              <td class="py-2 text-right">{model.conservative_score.toFixed(2)}</td>
              <td class="py-2 text-right">{model.games}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </Card>
  {/if}
</section>

{#if error}
  <p class="mt-4 text-xs text-muted-foreground">
    Could not load all KPIs ({error}). Some endpoints may require a spectator key.
  </p>
{/if}
