<script lang="ts">
  import { onMount } from 'svelte';
  import Card from '$lib/components/Card.svelte';
  import Button from '$lib/components/Button.svelte';
  import { padrino } from '$lib/clientStore.svelte';
  import type { GameListEntry } from '$lib/api/types';
  import { shortenHash } from '$lib/utils';

  let items = $state<GameListEntry[]>([]);
  let cursor = $state<string | null>(null);
  let nextCursor = $state<string | null>(null);
  let prevCursors = $state<(string | null)[]>([]);
  let statusFilter = $state<string>('');
  let loading = $state(false);
  let error = $state<string | null>(null);

  async function load() {
    loading = true;
    error = null;
    try {
      const response = await padrino.client.listGames({
        limit: 25,
        cursor,
        status: statusFilter === '' ? null : statusFilter
      });
      items = response.items;
      nextCursor = response.next_cursor;
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  }

  function applyFilter(value: string) {
    statusFilter = value;
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

<h1 class="mb-4 text-2xl font-semibold">Games</h1>

<div class="mb-4 flex gap-2">
  <Button variant={statusFilter === '' ? 'default' : 'outline'} onclick={() => applyFilter('')}>All</Button>
  <Button variant={statusFilter === 'RUNNING' ? 'default' : 'outline'} onclick={() => applyFilter('RUNNING')}>In flight</Button>
  <Button variant={statusFilter === 'COMPLETED' ? 'default' : 'outline'} onclick={() => applyFilter('COMPLETED')}>Completed</Button>
</div>

<Card>
  {#if loading}
    <p>Loading…</p>
  {:else if error}
    <p class="text-sm text-red-500">{error}</p>
  {:else if items.length === 0}
    <p class="text-sm text-muted-foreground">No games yet.</p>
  {:else}
    <table class="w-full text-sm">
      <thead class="text-left text-xs uppercase tracking-wider text-muted-foreground">
        <tr>
          <th class="pb-2">Game</th>
          <th class="pb-2">Status</th>
          <th class="pb-2">Phase</th>
          <th class="pb-2">Outcome</th>
          <th class="pb-2"></th>
        </tr>
      </thead>
      <tbody>
        {#each items as game (game.id)}
          <tr class="border-t border-border">
            <td class="py-2 font-mono text-xs">{shortenHash(game.id)}</td>
            <td class="py-2">{game.status}</td>
            <td class="py-2">{game.current_phase ?? '—'}</td>
            <td class="py-2">
              {game.terminal_result ? `${game.terminal_result.winner} — ${game.terminal_result.reason}` : '—'}
            </td>
            <td class="py-2 text-right">
              <a href={`/games/${game.id}`} class="text-sm underline">Open</a>
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
