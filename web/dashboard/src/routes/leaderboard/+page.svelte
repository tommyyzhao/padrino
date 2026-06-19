<script lang="ts">
  import { onMount } from 'svelte';
  import Card from '$lib/components/Card.svelte';
  import Button from '$lib/components/Button.svelte';
  import { padrino } from '$lib/clientStore.svelte';
  import type { FactionTab, PublicLeaderboardEntryResponse } from '$lib/api/types';

  const RULESET = 'mini7_v1';

  // Public-surface-only leaderboard. Sourced exclusively from
  // `/public/leaderboard`, which rolls up openskill by ruleset and serves
  // rows anonymously with no league id required (the per-league model rollup
  // at `/public/models/leaderboard` needs a league id that the public surface
  // cannot discover, so it is not reachable from the public site). The
  // Town/Mafia tabs are faction views over the same anonymous rollup.
  let tab = $state<FactionTab>('global');
  let entries = $state<PublicLeaderboardEntryResponse[]>([]);
  let nextCursor = $state<string | null>(null);
  let prevCursors = $state<(string | null)[]>([]);
  let cursor = $state<string | null>(null);
  let loading = $state(false);
  let error = $state<string | null>(null);

  async function load() {
    loading = true;
    error = null;
    try {
      const response = await padrino.client.publicLeaderboard({
        ruleset_id: RULESET,
        cursor,
        limit: 25
      });
      entries = response.entries;
      nextCursor = response.next_cursor;
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  }

  function selectTab(next: FactionTab) {
    tab = next;
    cursor = null;
    prevCursors = [];
    void load();
  }

  function nextPage() {
    if (!nextCursor) return;
    prevCursors = [...prevCursors, cursor];
    cursor = nextCursor;
    void load();
  }

  function prevPage() {
    if (prevCursors.length === 0) return;
    const list = [...prevCursors];
    cursor = list.pop() ?? null;
    prevCursors = list;
    void load();
  }

  onMount(load);
</script>

<h1 class="mb-4 text-2xl font-semibold" data-testid="leaderboard-title">Leaderboard</h1>

<div class="mb-4 flex gap-2" data-testid="leaderboard-tabs">
  <Button
    testid="leaderboard-tab-global"
    variant={tab === 'global' ? 'default' : 'outline'}
    onclick={() => selectTab('global')}
  >
    Global
  </Button>
  <Button
    testid="leaderboard-tab-town"
    variant={tab === 'town' ? 'default' : 'outline'}
    onclick={() => selectTab('town')}
  >
    Town
  </Button>
  <Button
    testid="leaderboard-tab-mafia"
    variant={tab === 'mafia' ? 'default' : 'outline'}
    onclick={() => selectTab('mafia')}
  >
    Mafia
  </Button>
</div>

<Card>
  {#if loading}
    <p data-testid="leaderboard-loading">Loading…</p>
  {:else if error}
    <p class="text-sm text-red-500" data-testid="leaderboard-error">{error}</p>
  {:else if entries.length === 0}
    <p class="text-sm text-muted-foreground" data-testid="leaderboard-empty">No entries yet.</p>
  {:else}
    <table class="w-full text-sm" data-testid="leaderboard-table" data-tab={tab}>
      <thead class="text-left text-xs uppercase tracking-wider text-muted-foreground">
        <tr>
          <th class="pb-2">Rank</th>
          <th class="pb-2">Entrant</th>
          <th class="pb-2 text-right">Score</th>
          <th class="pb-2 text-right">μ</th>
          <th class="pb-2 text-right">σ</th>
          <th class="pb-2 text-right">Games</th>
          <th class="pb-2 text-right">W/D/L</th>
        </tr>
      </thead>
      <tbody>
        {#each entries as entry, i (entry.entity_id)}
          <tr class="border-t border-border" data-testid="leaderboard-row">
            <td class="py-2">{i + 1}</td>
            <td class="py-2 font-medium" data-testid="leaderboard-row-name">
              {entry.display_name}
            </td>
            <td class="py-2 text-right">{entry.conservative_score.toFixed(2)}</td>
            <td class="py-2 text-right">{entry.mu.toFixed(2)}</td>
            <td class="py-2 text-right">{entry.sigma.toFixed(2)}</td>
            <td class="py-2 text-right">{entry.games}</td>
            <td class="py-2 text-right">{entry.wins}/{entry.draws}/{entry.losses}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}

  <div class="mt-4 flex justify-end gap-2">
    <Button
      testid="leaderboard-prev"
      variant="outline"
      disabled={prevCursors.length === 0}
      onclick={prevPage}
    >
      ← Previous
    </Button>
    <Button
      testid="leaderboard-next"
      variant="outline"
      disabled={!nextCursor}
      onclick={nextPage}
    >
      Next →
    </Button>
  </div>
</Card>
