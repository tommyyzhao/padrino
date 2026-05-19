<script lang="ts">
  import { page } from '$app/stores';
  import { onMount } from 'svelte';
  import Card from '$lib/components/Card.svelte';
  import Button from '$lib/components/Button.svelte';
  import { padrino } from '$lib/clientStore.svelte';
  import type { PublicEventEntry } from '$lib/api/types';
  import {
    currentGroup,
    initScrubber,
    jumpTo,
    next as scrubNext,
    prev as scrubPrev,
    projectEventForPublic,
    type ScrubberState
  } from '$lib/scrubber';
  import { shortenHash } from '$lib/utils';

  let gameId = $derived($page.params.id);
  let allEvents = $state<PublicEventEntry[]>([]);
  let scrubberState = $state<ScrubberState>({ groups: [], currentIndex: -1 });
  let nextCursor = $state<string | null>(null);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let isTerminal = $state(false);

  async function loadInitial() {
    if (!gameId) return;
    loading = true;
    error = null;
    try {
      const response = await padrino.client.publicGameEvents(gameId, { limit: 200 });
      allEvents = response.items;
      nextCursor = response.next_cursor;
      const terminalEvent = response.items.find((e) => e.event_type === 'GameTerminated');
      isTerminal = terminalEvent !== undefined;
      scrubberState = initScrubber(allEvents);
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  }

  async function loadMore() {
    if (!nextCursor || !gameId) return;
    loading = true;
    try {
      const response = await padrino.client.publicGameEvents(gameId, {
        cursor: nextCursor,
        limit: 200
      });
      allEvents = [...allEvents, ...response.items];
      nextCursor = response.next_cursor;
      const terminalEvent = response.items.find((e) => e.event_type === 'GameTerminated');
      if (terminalEvent) isTerminal = true;
      scrubberState = initScrubber(allEvents);
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  }

  const group = $derived(currentGroup(scrubberState));
  const projectedEvents = $derived(group ? group.events.map((e) => projectEventForPublic(e, isTerminal)) : []);

  onMount(loadInitial);
</script>

<div class="mb-4">
  <a class="text-sm underline" href="/games">← Games</a>
</div>

<h1 class="mb-2 text-xl font-semibold" data-testid="replay-title">Replay</h1>
<p class="mb-4 font-mono text-xs text-muted-foreground" data-testid="replay-game-id">
  {gameId}
</p>

{#if loading && allEvents.length === 0}
  <p data-testid="replay-loading">Loading…</p>
{:else if error}
  <p class="text-sm text-red-500" data-testid="replay-error">{error}</p>
{:else}
  <div class="mb-4 grid gap-3 sm:grid-cols-[200px_1fr]" data-testid="replay-shell">
    <Card class="self-start">
      <h2 class="mb-2 text-sm font-semibold">Phases</h2>
      {#if scrubberState.groups.length === 0}
        <p class="text-xs text-muted-foreground" data-testid="replay-phase-empty">No events.</p>
      {:else}
        <ul class="flex flex-col gap-1 text-sm" data-testid="replay-phase-list">
          {#each scrubberState.groups as g (g.phase)}
            <li>
              <button
                class={'w-full rounded-md px-2 py-1 text-left text-xs ' +
                  (scrubberState.currentIndex === g.index
                    ? 'bg-primary text-background'
                    : 'hover:bg-accent')}
                data-testid="replay-phase-pill"
                data-phase={g.phase}
                data-active={scrubberState.currentIndex === g.index}
                onclick={() => (scrubberState = jumpTo(scrubberState, g.phase))}
              >
                {g.phase}
              </button>
            </li>
          {/each}
        </ul>
      {/if}
    </Card>

    <Card>
      <div class="mb-3 flex items-center justify-between">
        <div class="text-sm font-medium" data-testid="replay-current-phase">
          {group ? group.phase : '—'}
          <span class="text-xs text-muted-foreground" data-testid="replay-position"
            >({(group?.index ?? 0) + 1} / {scrubberState.groups.length})</span
          >
        </div>
        <div class="flex gap-2">
          <Button
            testid="replay-prev"
            variant="outline"
            disabled={scrubberState.currentIndex <= 0}
            onclick={() => (scrubberState = scrubPrev(scrubberState))}
          >
            ←
          </Button>
          <Button
            testid="replay-next"
            variant="outline"
            disabled={scrubberState.currentIndex >= scrubberState.groups.length - 1}
            onclick={() => (scrubberState = scrubNext(scrubberState))}
          >
            →
          </Button>
        </div>
      </div>

      {#if !isTerminal}
        <p
          class="mb-3 rounded-md border border-border bg-muted px-3 py-2 text-xs"
          data-testid="replay-in-flight-warning"
        >
          Game is still in flight — role-revealing events are hidden until terminal.
        </p>
      {/if}

      {#if projectedEvents.length === 0}
        <p class="text-sm text-muted-foreground" data-testid="replay-events-empty">
          No events in this phase.
        </p>
      {:else}
        <ol class="flex flex-col gap-2 text-sm" data-testid="replay-event-list">
          {#each projectedEvents as ev (ev.event_hash)}
            <li class="rounded-md border border-border p-2 text-xs" data-testid="replay-event">
              <div class="flex items-center justify-between">
                <span class="font-mono" data-testid="replay-event-type">{ev.event_type}</span>
                <span class="text-muted-foreground"
                  >#{ev.sequence} · {shortenHash(ev.event_hash)}</span
                >
              </div>
              {#if ev.actor_player_id}
                <div class="text-muted-foreground">actor: {ev.actor_player_id}</div>
              {/if}
              <pre class="mt-1 whitespace-pre-wrap break-words text-[10px]">{JSON.stringify(
                  ev.payload,
                  null,
                  2
                )}</pre>
            </li>
          {/each}
        </ol>
      {/if}

      {#if nextCursor}
        <div class="mt-3 flex justify-center">
          <Button testid="replay-load-more" variant="outline" onclick={loadMore}>
            Load more events
          </Button>
        </div>
      {/if}
    </Card>
  </div>
{/if}
