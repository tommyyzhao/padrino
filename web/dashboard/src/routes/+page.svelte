<script lang="ts">
  import { onMount } from 'svelte';
  import Card from '$lib/components/Card.svelte';
  import { padrino } from '$lib/clientStore.svelte';
  import type {
    PublicModelEntryResponse,
    PublicLiveGameEntry,
    PublicRecentGameEntry
  } from '$lib/api/types';

  const RULESET = 'mini7_v1';

  let totalGames = $state<number | null>(null);
  let activeGauntlets = $state<number | null>(null);
  let topModels = $state<PublicModelEntryResponse[]>([]);
  let error = $state<string | null>(null);

  let liveGames = $state<PublicLiveGameEntry[]>([]);
  let recentGames = $state<PublicRecentGameEntry[]>([]);

  async function load() {
    // The five KPI fetches are independent — run them concurrently and keep
    // each one's degrade-on-failure semantics via allSettled.
    const [lbResult, gamesResult, runningResult, liveResult, recentResult] =
      await Promise.allSettled([
        // Top models: requires a league id; pick the first available league via
        // the public surface indirectly — for the home KPI we accept a fallback
        // to the global public leaderboard (entity-keyed, identity-blind).
        padrino.client.publicLeaderboard({ ruleset_id: RULESET, limit: 3 }),
        padrino.client.listGames({ limit: 1 }),
        padrino.client.listGauntlets({ status: 'RUNNING', limit: 50 }),
        padrino.client.publicLiveIndex(),
        padrino.client.publicRecentIndex({ limit: 10 })
      ]);

    if (lbResult.status === 'fulfilled') {
      topModels = lbResult.value.entries.slice(0, 3).map((e) => ({
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
    } else {
      // Anonymous mode may not be enabled; degrade silently for KPIs.
      error = (lbResult.reason as Error).message;
    }

    totalGames =
      gamesResult.status === 'fulfilled'
        ? (gamesResult.value.total_estimate ?? gamesResult.value.items.length)
        : null;

    activeGauntlets = runningResult.status === 'fulfilled' ? runningResult.value.items.length : null;

    if (liveResult.status === 'fulfilled') {
      liveGames = liveResult.value.items;
    }
    // silent on failure: broadcast surface is optional

    if (recentResult.status === 'fulfilled') {
      recentGames = recentResult.value.items;
    }
  }

  onMount(load);
</script>

<div class="grid gap-4 sm:grid-cols-3" data-testid="home-kpis">
  <Card>
    <div class="text-xs uppercase tracking-wider text-muted-foreground">Total games</div>
    <div class="mt-1 text-3xl font-semibold" data-testid="home-kpi-total-games">
      {totalGames ?? '—'}
    </div>
  </Card>
  <Card>
    <div class="text-xs uppercase tracking-wider text-muted-foreground">Active gauntlets</div>
    <div class="mt-1 text-3xl font-semibold" data-testid="home-kpi-active-gauntlets">
      {activeGauntlets ?? '—'}
    </div>
  </Card>
  <Card>
    <div class="text-xs uppercase tracking-wider text-muted-foreground">Top model</div>
    <div class="mt-1 text-xl font-semibold" data-testid="home-kpi-top-model">
      {topModels[0]?.display_name ?? '—'}
    </div>
  </Card>
</div>

<section class="mt-8" data-testid="home-top-models">
  <h2 class="mb-3 text-lg font-semibold">Top 3 models</h2>
  {#if topModels.length === 0}
    <p class="text-sm text-muted-foreground" data-testid="home-top-models-empty">
      No ranked results yet.
    </p>
  {:else}
    <Card>
      <table class="w-full text-sm" data-testid="home-top-models-table">
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
            <tr class="border-t border-border" data-testid="home-top-model-row">
              <td class="py-2">{i + 1}</td>
              <td class="py-2 font-medium">
                <a
                  href="/leaderboard"
                  class="underline-offset-2 hover:underline"
                  data-testid="home-top-model-link"
                >
                  {model.display_name}
                </a>
              </td>
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

<section class="mt-8" data-testid="lobby-live-section">
  <h2 class="mb-3 text-lg font-semibold">Live Now</h2>
  {#if liveGames.length === 0}
    <p class="text-sm text-muted-foreground" data-testid="lobby-live-empty">No games live right now.</p>
  {:else}
    <ul class="grid gap-3 sm:grid-cols-2" data-testid="lobby-live-list">
      {#each liveGames as game (game.game_id)}
        <li>
          <a href="/watch/{game.game_id}" class="block" data-testid="lobby-live-card">
            <Card>
              <div class="font-mono text-xs text-muted-foreground mb-1">{game.game_id.slice(0, 8)}</div>
              <div class="font-medium" data-testid="lobby-live-ruleset">{game.ruleset_id}</div>
              {#if game.current_phase}
                <div class="mt-1 text-sm text-muted-foreground" data-testid="lobby-live-phase">{game.current_phase}</div>
              {/if}
              <div class="mt-1 text-sm" data-testid="lobby-live-players">{game.players_alive} alive</div>
              <span class="mt-2 inline-block rounded bg-emerald-500/20 px-2 py-0.5 text-xs text-emerald-600">Live</span>
            </Card>
          </a>
        </li>
      {/each}
    </ul>
  {/if}
</section>

<section class="mt-8" data-testid="lobby-recent-section">
  <h2 class="mb-3 text-lg font-semibold">Recently Finished</h2>
  {#if recentGames.length === 0}
    <p class="text-sm text-muted-foreground" data-testid="lobby-recent-empty">No recent games.</p>
  {:else}
    <ul class="grid gap-3 sm:grid-cols-2" data-testid="lobby-recent-list">
      {#each recentGames as game (game.game_id)}
        <li>
          <a href="/watch/{game.game_id}" class="block" data-testid="lobby-recent-card">
            <Card>
              <div class="font-mono text-xs text-muted-foreground mb-1">{game.game_id.slice(0, 8)}</div>
              <div class="font-medium" data-testid="lobby-recent-ruleset">{game.ruleset_id}</div>
              {#if game.terminal_result}
                <div class="mt-1 text-sm font-semibold" data-testid="lobby-recent-winner">
                  Winner: {String(game.terminal_result.winner)}
                </div>
              {/if}
            </Card>
          </a>
        </li>
      {/each}
    </ul>
  {/if}
</section>
