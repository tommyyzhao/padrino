<script lang="ts">
  import { page } from '$app/stores';
  import { onMount } from 'svelte';
  import Card from '$lib/components/Card.svelte';
  import { padrino } from '$lib/clientStore.svelte';
  import type { PublicLadderEntry, PublicRecentGameEntry } from '$lib/api/types';

  const RULESETS = ['mini7_v1'];

  let agentBuildId = $derived($page.params.id);

  let agent = $state<PublicLadderEntry | null>(null);
  let recentGames = $state<PublicRecentGameEntry[]>([]);
  let loading = $state(false);
  let error = $state<string | null>(null);

  async function load() {
    loading = true;
    error = null;
    try {
      // Load ladder across all rulesets to find this agent's entry
      const results = await Promise.all(
        RULESETS.map((r) => padrino.client.publicLadder({ ruleset_id: r, limit: 100 }))
      );
      for (const resp of results) {
        const found = resp.entries.find((e) => e.agent_build_id === agentBuildId);
        if (found) {
          agent = found;
          break;
        }
      }
    } catch (e) {
      error = (e as Error).message;
    }

    try {
      const resp = await padrino.client.publicRecentIndex({ limit: 20 });
      recentGames = resp.items;
    } catch {
      // recent games are supplementary; fail silently
    }

    loading = false;
  }

  onMount(() => {
    void load();
  });
</script>

<div class="mb-4">
  <a class="text-sm underline" href="/ladder">← Ladder</a>
</div>

{#if loading}
  <p class="text-sm text-muted-foreground" data-testid="model-loading">Loading…</p>
{:else if error}
  <p class="text-sm text-red-500" data-testid="model-error">{error}</p>
{:else if agent === null}
  <p class="text-sm text-muted-foreground" data-testid="model-not-found">Agent not found.</p>
{:else}
  <div class="mb-6">
    <h1 class="text-xl font-semibold" data-testid="model-display-name">{agent.display_name}</h1>
    <p class="mt-1 font-mono text-xs text-muted-foreground" data-testid="model-build-id">
      {agentBuildId}
    </p>
  </div>

  <div class="mb-6 grid gap-4 sm:grid-cols-3">
    <Card>
      <div class="text-xs uppercase tracking-wider text-muted-foreground">Ordinal</div>
      <div class="mt-1 text-3xl font-semibold" data-testid="model-ordinal">{agent.ordinal}</div>
      {#if agent.provisional}
        <span
          class="mt-1 inline-block rounded bg-amber-100 px-2 py-0.5 text-xs text-amber-700"
          data-testid="model-provisional-badge"
        >
          provisional
        </span>
      {:else}
        <span
          class="mt-1 inline-block rounded bg-emerald-100 px-2 py-0.5 text-xs text-emerald-700"
          data-testid="model-established-badge"
        >
          established
        </span>
      {/if}
    </Card>
    <Card>
      <div class="text-xs uppercase tracking-wider text-muted-foreground">Games</div>
      <div class="mt-1 text-3xl font-semibold" data-testid="model-games">{agent.games}</div>
    </Card>
    <Card>
      <div class="text-xs uppercase tracking-wider text-muted-foreground">Version</div>
      <div class="mt-1 font-mono text-lg font-semibold" data-testid="model-version">
        {agent.version}
      </div>
    </Card>
  </div>
{/if}

<section class="mt-4" data-testid="model-recent-games">
  <h2 class="mb-3 text-lg font-semibold">Recent Games</h2>
  {#if recentGames.length === 0}
    <p class="text-sm text-muted-foreground" data-testid="model-recent-empty">No recent games.</p>
  {:else}
    <ul class="grid gap-3 sm:grid-cols-2">
      {#each recentGames as game (game.game_id)}
        <li>
          <a href="/watch/{game.game_id}" class="block" data-testid="model-recent-game-card">
            <Card>
              <div class="font-mono text-xs text-muted-foreground">{game.game_id.slice(0, 8)}</div>
              <div class="mt-1 text-sm font-medium">{game.ruleset_id}</div>
              {#if game.terminal_result}
                <div class="mt-1 text-xs text-muted-foreground">
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
