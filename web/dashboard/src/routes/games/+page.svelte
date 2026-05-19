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

<h1 class="mb-4 text-2xl font-semibold" data-testid="games-title">Games</h1>

<div class="mb-4 flex gap-2" data-testid="games-filters">
  <Button
    testid="games-filter-all"
    variant={statusFilter === '' ? 'default' : 'outline'}
    onclick={() => applyFilter('')}
  >
    All
  </Button>
  <Button
    testid="games-filter-running"
    variant={statusFilter === 'RUNNING' ? 'default' : 'outline'}
    onclick={() => applyFilter('RUNNING')}
  >
    In flight
  </Button>
  <Button
    testid="games-filter-completed"
    variant={statusFilter === 'COMPLETED' ? 'default' : 'outline'}
    onclick={() => applyFilter('COMPLETED')}
  >
    Completed
  </Button>
</div>

<Card>
  {#if loading}
    <p data-testid="games-loading">Loading…</p>
  {:else if error}
    <p class="text-sm text-red-500" data-testid="games-error">{error}</p>
  {:else if items.length === 0}
    <p class="text-sm text-muted-foreground" data-testid="games-empty">No games yet.</p>
  {:else}
    <table class="w-full text-sm" data-testid="games-table" data-status={statusFilter}>
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
          <tr class="border-t border-border" data-testid="games-row" data-game-id={game.id}>
            <td class="py-2 font-mono text-xs">{shortenHash(game.id)}</td>
            <td class="py-2" data-testid="games-row-status">{game.status}</td>
            <td class="py-2">{game.current_phase ?? '—'}</td>
            <td class="py-2">
              {game.terminal_result ? `${game.terminal_result.winner} — ${game.terminal_result.reason}` : '—'}
            </td>
            <td class="py-2 text-right">
              <a
                href={`/games/${game.id}`}
                class="text-sm underline"
                data-testid="games-open-link"
              >
                Open
              </a>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}

  <div class="mt-4 flex justify-end gap-2">
    <Button
      testid="games-prev"
      variant="outline"
      disabled={prevCursors.length === 0}
      onclick={prevPage}
    >
      ← Previous
    </Button>
    <Button
      testid="games-next"
      variant="outline"
      disabled={!nextCursor}
      onclick={nextPage}
    >
      Next →
    </Button>
  </div>
</Card>
