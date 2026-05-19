<script lang="ts">
  import { onMount } from 'svelte';
  import Card from '$lib/components/Card.svelte';
  import Button from '$lib/components/Button.svelte';
  import { padrino } from '$lib/clientStore.svelte';
  import type { FactionTab, PublicModelEntryResponse } from '$lib/api/types';

  const RULESET = 'mini7_v1';

  let leagueId = $state<string>('');
  let tab = $state<FactionTab>('global');
  let entries = $state<PublicModelEntryResponse[]>([]);
  let nextCursor = $state<string | null>(null);
  let prevCursors = $state<(string | null)[]>([]);
  let cursor = $state<string | null>(null);
  let loading = $state(false);
  let error = $state<string | null>(null);

  async function discoverLeague() {
    // The public leaderboard endpoint exposes per-league rollups; we discover
    // the league id by listing gauntlets and reading the league_id off the
    // newest one.
    if (leagueId) return;
    try {
      const gauntlets = await padrino.client.listGauntlets({ limit: 1 });
      if (gauntlets.items.length > 0) {
        leagueId = gauntlets.items[0].league_id;
      }
    } catch (e) {
      error = (e as Error).message;
    }
  }

  async function load() {
    if (!leagueId) await discoverLeague();
    if (!leagueId) {
      error = 'No league found yet — submit a gauntlet to populate the leaderboard.';
      return;
    }
    loading = true;
    error = null;
    try {
      const response = await padrino.client.publicModelLeaderboard({
        ruleset_id: RULESET,
        league_id: leagueId,
        cursor,
        limit: 25
      });
      entries = sortForTab(response.entries, tab);
      nextCursor = response.next_cursor;
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  }

  function sortForTab(items: PublicModelEntryResponse[], current: FactionTab): PublicModelEntryResponse[] {
    if (current === 'global') return items;
    return [...items].sort((a, b) => {
      const aScore = current === 'town' ? a.town.conservative_score : a.mafia.conservative_score;
      const bScore = current === 'town' ? b.town.conservative_score : b.mafia.conservative_score;
      return bScore - aScore;
    });
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

<h1 class="mb-4 text-2xl font-semibold">Model leaderboard</h1>

<div class="mb-4 flex gap-2">
  <Button variant={tab === 'global' ? 'default' : 'outline'} onclick={() => selectTab('global')}>Global</Button>
  <Button variant={tab === 'town' ? 'default' : 'outline'} onclick={() => selectTab('town')}>Town</Button>
  <Button variant={tab === 'mafia' ? 'default' : 'outline'} onclick={() => selectTab('mafia')}>Mafia</Button>
</div>

<Card>
  {#if loading}
    <p>Loading…</p>
  {:else if error}
    <p class="text-sm text-red-500">{error}</p>
  {:else if entries.length === 0}
    <p class="text-sm text-muted-foreground">No entries yet.</p>
  {:else}
    <table class="w-full text-sm">
      <thead class="text-left text-xs uppercase tracking-wider text-muted-foreground">
        <tr>
          <th class="pb-2">Rank</th>
          <th class="pb-2">Model</th>
          <th class="pb-2 text-right">Score</th>
          <th class="pb-2 text-right">μ</th>
          <th class="pb-2 text-right">σ</th>
          <th class="pb-2 text-right">Games</th>
          <th class="pb-2 text-right">W/D/L</th>
        </tr>
      </thead>
      <tbody>
        {#each entries as entry, i (entry.model_key)}
          {@const facet = tab === 'town' ? entry.town : tab === 'mafia' ? entry.mafia : null}
          <tr class="border-t border-border">
            <td class="py-2">{i + 1}</td>
            <td class="py-2 font-medium">{entry.display_name}</td>
            <td class="py-2 text-right">
              {(facet ?? entry).conservative_score.toFixed(2)}
            </td>
            <td class="py-2 text-right">{(facet ?? entry).mu.toFixed(2)}</td>
            <td class="py-2 text-right">{(facet ?? entry).sigma.toFixed(2)}</td>
            <td class="py-2 text-right">{(facet ?? entry).games}</td>
            <td class="py-2 text-right">
              {(facet ?? entry).wins}/{(facet ?? entry).draws}/{(facet ?? entry).losses}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}

  <div class="mt-4 flex justify-end gap-2">
    <Button variant="outline" disabled={prevCursors.length === 0} onclick={prevPage}>← Previous</Button>
    <Button variant="outline" disabled={!nextCursor} onclick={nextPage}>Next →</Button>
  </div>
</Card>
