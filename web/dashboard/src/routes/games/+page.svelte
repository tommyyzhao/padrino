<script lang="ts">
  import { onMount } from 'svelte';
  import Card from '$lib/components/Card.svelte';
  import Button from '$lib/components/Button.svelte';
  import { padrino } from '$lib/clientStore.svelte';
  import { shortenHash } from '$lib/utils';

  // Public games browser: sourced exclusively from the public spectator surface
  // (/public/live + /public/recent) so it works against a public-surface-only
  // API. LIVE games come from /public/live; finished games from /public/recent.
  interface BrowserRow {
    id: string;
    status: string;
    current_phase: string | null;
    outcome: string | null;
    watchHref: string;
  }

  let liveRows = $state<BrowserRow[]>([]);
  let recentRows = $state<BrowserRow[]>([]);
  let statusFilter = $state<string>('');
  let loading = $state(false);
  let error = $state<string | null>(null);

  async function load() {
    loading = true;
    error = null;
    try {
      const [live, recent] = await Promise.all([
        padrino.client.publicLiveIndex(),
        padrino.client.publicRecentIndex({ limit: 50 })
      ]);
      liveRows = live.items.map((g) => ({
        id: g.game_id,
        status: 'RUNNING',
        current_phase: g.current_phase,
        outcome: null,
        watchHref: `/watch/${g.game_id}`
      }));
      recentRows = recent.items.map((g) => ({
        id: g.game_id,
        status: 'COMPLETED',
        current_phase: g.current_phase,
        outcome: g.terminal_result
          ? `${g.terminal_result.winner} — ${g.terminal_result.reason}`
          : null,
        watchHref: `/games/${g.game_id}`
      }));
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  }

  const items = $derived(
    statusFilter === 'RUNNING'
      ? liveRows
      : statusFilter === 'COMPLETED'
        ? recentRows
        : [...liveRows, ...recentRows]
  );

  function applyFilter(value: string) {
    statusFilter = value;
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
            <td class="py-2">{game.outcome ?? '—'}</td>
            <td class="py-2 text-right">
              <a
                href={game.watchHref}
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
</Card>
