<script lang="ts">
  import { page } from '$app/stores';
  import { onMount, onDestroy } from 'svelte';
  import Card from '$lib/components/Card.svelte';
  import { padrino } from '$lib/clientStore.svelte';

  interface PublicFrame {
    schema_version: string;
    sequence: number;
    event_type: string;
    phase: string;
    visibility: string;
    actor_player_id: string | null;
    payload: Record<string, unknown>;
    prev_event_hash: string;
    event_hash: string;
  }

  interface ChatEntry {
    sequence: number;
    phase: string;
    actor: string;
    text: string;
  }

  let gameId = $derived($page.params.id);

  let frameCount = $state(0);
  let currentPhase = $state<string>('—');
  // player_id -> alive
  let playerAlive = $state<Record<string, boolean>>({});
  let chatEntries = $state<ChatEntry[]>([]);
  // voter_id -> target_id
  let votesByVoter = $state<Record<string, string>>({});
  let terminalResult = $state<Record<string, unknown> | null>(null);

  let connected = $state(false);
  let error = $state<string | null>(null);

  let sse: EventSource | null = null;

  function processFrame(frame: PublicFrame): void {
    currentPhase = frame.phase;

    // Track any actor as alive if first seen
    if (frame.actor_player_id !== null && !(frame.actor_player_id in playerAlive)) {
      playerAlive = { ...playerAlive, [frame.actor_player_id]: true };
    }

    const payload = frame.payload as Record<string, unknown>;

    switch (frame.event_type) {
      case 'PlayerEliminated': {
        const pid = frame.actor_player_id;
        if (pid !== null) {
          playerAlive = { ...playerAlive, [pid]: false };
        }
        break;
      }
      case 'PublicMessageSubmitted': {
        const actor = frame.actor_player_id;
        const text = typeof payload['text'] === 'string' ? payload['text'] : null;
        if (actor !== null && text !== null) {
          chatEntries = [
            ...chatEntries,
            { sequence: frame.sequence, phase: frame.phase, actor, text }
          ];
        }
        break;
      }
      case 'VoteCast': {
        const voter = frame.actor_player_id;
        const target =
          typeof payload['target_player_id'] === 'string' ? payload['target_player_id'] : null;
        if (voter !== null && target !== null) {
          votesByVoter = { ...votesByVoter, [voter]: target };
        }
        break;
      }
      case 'DayVoteResolved':
      case 'PhaseResolved': {
        votesByVoter = {};
        break;
      }
      case 'GameTerminated': {
        terminalResult = payload;
        sse?.close();
        connected = false;
        break;
      }
    }
  }

  function connect(): void {
    if (!gameId) return;
    const url = `${padrino.baseUrl}/public/games/${encodeURIComponent(gameId)}/live`;
    sse = new EventSource(url);

    sse.onopen = () => {
      connected = true;
      error = null;
    };

    sse.onmessage = (evt: MessageEvent) => {
      try {
        const frame = JSON.parse(evt.data as string) as PublicFrame;
        frameCount += 1;
        processFrame(frame);
      } catch {
        // ignore malformed frames
      }
    };

    sse.onerror = () => {
      connected = false;
    };
  }

  onMount(() => {
    connect();
  });

  onDestroy(() => {
    sse?.close();
  });

  const voteTally = $derived(
    (() => {
      const tally: Record<string, number> = {};
      for (const target of Object.values(votesByVoter)) {
        tally[target] = (tally[target] ?? 0) + 1;
      }
      return Object.entries(tally)
        .sort((a, b) => b[1] - a[1])
        .map(([player, count]) => ({ player, count }));
    })()
  );

  const seats = $derived(
    Object.entries(playerAlive).map(([id, alive]) => ({ id, alive }))
  );

  const aliveCount = $derived(seats.filter((s) => s.alive).length);
</script>

<div class="mb-4">
  <a class="text-sm underline" href="/">← Home</a>
</div>

<div class="mb-2 flex items-center gap-3">
  <h1 class="text-xl font-semibold" data-testid="watch-title">Live Match</h1>
  {#if terminalResult !== null}
    <span
      class="rounded bg-muted px-2 py-0.5 font-mono text-xs text-muted-foreground"
      data-testid="watch-status"
    >
      Ended
    </span>
  {:else if connected}
    <span
      class="rounded bg-emerald-500/20 px-2 py-0.5 font-mono text-xs text-emerald-600"
      data-testid="watch-status"
    >
      Live
    </span>
  {:else}
    <span
      class="rounded bg-muted px-2 py-0.5 font-mono text-xs text-muted-foreground"
      data-testid="watch-status"
    >
      Connecting…
    </span>
  {/if}
</div>

<p class="mb-4 font-mono text-xs text-muted-foreground" data-testid="watch-game-id">{gameId}</p>

{#if error}
  <p class="mb-4 text-sm text-red-500" data-testid="watch-error">{error}</p>
{/if}

{#if terminalResult !== null}
  <div
    class="mb-4 rounded-md border border-emerald-300 bg-emerald-50 px-4 py-3 text-sm"
    data-testid="watch-outcome-banner"
    data-winner={String(terminalResult['winner'] ?? '')}
  >
    <span class="font-semibold">Game over.</span>
    {#if terminalResult['winner']}
      Winner: <strong data-testid="watch-winner">{String(terminalResult['winner'])}</strong>
    {/if}
    {#if terminalResult['reason']}
      — {String(terminalResult['reason'])}
    {/if}
  </div>
{/if}

<div class="grid gap-3 sm:grid-cols-[180px_1fr]" data-testid="watch-shell">
  <div class="flex flex-col gap-3">
    <Card>
      <h2 class="mb-2 text-sm font-semibold">Phase</h2>
      <p class="font-mono text-xs" data-testid="watch-phase">{currentPhase}</p>
      {#if aliveCount > 0}
        <p class="mt-1 text-xs text-muted-foreground" data-testid="watch-alive-count">
          {aliveCount} alive
        </p>
      {/if}
    </Card>

    <Card>
      <h2 class="mb-2 text-sm font-semibold">Seats</h2>
      {#if seats.length === 0}
        <p class="text-xs text-muted-foreground" data-testid="watch-seats-empty">Waiting…</p>
      {:else}
        <ul class="flex flex-col gap-1" data-testid="watch-seat-grid">
          {#each seats as seat (seat.id)}
            <li
              class={'flex items-center gap-2 rounded px-1 py-0.5 text-xs ' +
                (seat.alive ? '' : 'text-muted-foreground line-through')}
              data-testid="watch-seat-row"
              data-player-id={seat.id}
              data-alive={String(seat.alive)}
            >
              <span class={seat.alive ? 'text-emerald-500' : 'text-muted-foreground'}>
                {seat.alive ? '●' : '○'}
              </span>
              <span class="font-mono">{seat.id.slice(0, 8)}</span>
            </li>
          {/each}
        </ul>
      {/if}
    </Card>

    {#if voteTally.length > 0}
      <Card>
        <h2 class="mb-2 text-sm font-semibold">Votes</h2>
        <ul class="flex flex-col gap-1" data-testid="watch-vote-tally">
          {#each voteTally as v (v.player)}
            <li
              class="flex items-center justify-between text-xs"
              data-testid="watch-vote-row"
              data-player={v.player}
            >
              <span class="font-mono">{v.player.slice(0, 8)}</span>
              <span class="font-semibold">{v.count}</span>
            </li>
          {/each}
        </ul>
      </Card>
    {/if}
  </div>

  <Card>
    <h2 class="mb-2 text-sm font-semibold">Chat</h2>
    {#if chatEntries.length === 0}
      <p class="text-xs text-muted-foreground" data-testid="watch-chat-empty">
        {#if frameCount === 0}Waiting for stream…{:else}No chat yet.{/if}
      </p>
    {:else}
      <ol class="flex flex-col gap-2" data-testid="watch-chat-feed">
        {#each chatEntries as entry (entry.sequence)}
          <li class="text-xs" data-testid="watch-chat-entry">
            <span class="font-mono text-muted-foreground">[{entry.phase}]</span>
            <span class="mx-1 font-semibold">{entry.actor.slice(0, 8)}</span>
            <span>{entry.text}</span>
          </li>
        {/each}
      </ol>
    {/if}
  </Card>
</div>
