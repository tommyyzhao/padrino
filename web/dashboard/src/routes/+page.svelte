<script lang="ts">
  import { onMount } from 'svelte';
  import Card from '$lib/components/Card.svelte';
  import { padrino } from '$lib/clientStore.svelte';
  import type {
    PublicLadderEntry,
    PublicLiveGameEntry,
    PublicRecentGameEntry
  } from '$lib/api/types';

  const RULESET = 'mini7_v1';

  // KPIs are sourced exclusively from the public spectator surface so the home
  // page works against a public-surface-only API (no private /games, /gauntlets).
  let liveNow = $state<number | null>(null);
  let recentCount = $state<number | null>(null);
  let topAgents = $state<PublicLadderEntry[]>([]);

  let liveGames = $state<PublicLiveGameEntry[]>([]);
  let recentGames = $state<PublicRecentGameEntry[]>([]);

  async function load() {
    // Three public surfaces feed both the KPIs and the lobby sections.
    // Run them concurrently; each degrades independently on failure so a single
    // unavailable surface never blocks the page or shows a hard error.
    const [liveResult, recentResult, ladderResult] = await Promise.allSettled([
      padrino.client.publicLiveIndex(),
      padrino.client.publicRecentIndex({ limit: 10 }),
      padrino.client.publicLadder({ ruleset_id: RULESET, limit: 3 })
    ]);

    if (liveResult.status === 'fulfilled') {
      liveGames = liveResult.value.items;
      liveNow = liveResult.value.total;
    }

    if (recentResult.status === 'fulfilled') {
      recentGames = recentResult.value.items;
      recentCount = recentResult.value.total_estimate;
    }

    if (ladderResult.status === 'fulfilled') {
      topAgents = ladderResult.value.entries.slice(0, 3);
    }
  }

  onMount(load);
</script>

<div class="grid gap-4 sm:grid-cols-3" data-testid="home-kpis">
  <Card>
    <div class="text-xs uppercase tracking-wider text-muted-foreground">Live now</div>
    <div class="mt-1 text-3xl font-semibold" data-testid="home-kpi-live-now">
      {liveNow ?? '—'}
    </div>
  </Card>
  <Card>
    <div class="text-xs uppercase tracking-wider text-muted-foreground">Recent games</div>
    <div class="mt-1 text-3xl font-semibold" data-testid="home-kpi-recent-games">
      {recentCount ?? '—'}
    </div>
  </Card>
  <Card>
    <div class="text-xs uppercase tracking-wider text-muted-foreground">Top agent</div>
    <div class="mt-1 text-xl font-semibold" data-testid="home-kpi-top-agent">
      {topAgents[0]?.display_name ?? '—'}
    </div>
  </Card>
</div>

<section class="mt-8" data-testid="home-top-agents">
  <div class="mb-3 flex items-center justify-between gap-3">
    <h2 class="text-lg font-semibold">Top 3 agents</h2>
    <a
      href="/leaderboard"
      class="text-sm underline-offset-2 hover:underline"
      data-testid="home-leaderboard-link"
    >
      Leaderboard
    </a>
  </div>
  {#if topAgents.length === 0}
    <p class="text-sm text-muted-foreground" data-testid="home-top-agents-empty">
      No ranked results yet.
    </p>
  {:else}
    <Card>
      <table class="w-full text-sm" data-testid="home-top-agents-table">
        <thead class="text-left text-xs uppercase tracking-wider text-muted-foreground">
          <tr>
            <th class="pb-2">Rank</th>
            <th class="pb-2">Agent</th>
            <th class="pb-2 text-right">Ordinal</th>
            <th class="pb-2 text-right">Games</th>
          </tr>
        </thead>
        <tbody>
          {#each topAgents as agent, i (agent.agent_build_id)}
            <tr class="border-t border-border" data-testid="home-top-agent-row">
              <td class="py-2">{i + 1}</td>
              <td class="py-2 font-medium">
                <a
                  href="/ladder"
                  class="underline-offset-2 hover:underline"
                  data-testid="home-top-agent-link"
                >
                  {agent.display_name}
                </a>
              </td>
              <td class="py-2 text-right">{agent.ordinal}</td>
              <td class="py-2 text-right">{agent.games}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </Card>
  {/if}
</section>

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
