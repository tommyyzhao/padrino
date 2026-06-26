<script lang="ts">
  // US-155: In-game play surface.
  //
  // /play/[gameId]: an identity-blind, count-only play board with a non-precise
  // phase countdown, legal-action-gated action / vote / night panels, and a
  // buffered chat composer whose feed is fed ONLY by RELEASED frames. The
  // buffered hold + symmetric release delay + moderation live server-side
  // (US-138/140), so a frame that reaches this client is already released. The
  // seat sprite key is resolved server-side to a role-agnostic archetype in
  // anonymous mode (US-152), so this surface can never encode role or human/AI
  // (AGENTS.md rule 7).
  import { page } from '$app/stores';
  import { onMount, onDestroy } from 'svelte';
  import Card from '$lib/components/Card.svelte';
  import Button from '$lib/components/Button.svelte';
  import HowToPlayPanel from '$lib/components/HowToPlayPanel.svelte';
  import { padrino } from '$lib/clientStore.svelte';
  import { createPlaySession, type PlaySession } from '$lib/playSession.svelte';
  import { newIdempotencyKey } from '$lib/api/liveClient';
  import { deriveVoteTally } from '$lib/api/playState';
  import {
    actionTypeLabel,
    actionTypeDescription,
    composerStatusFromChat,
    countdownBucket,
    countdownLabel,
    isNightActionPhase,
    isVotePhase,
    nightActionType,
    secondsUntil,
    spriteUrl,
    type ComposerStatus
  } from '$lib/api/playSurface';
  import type {
    CompositionSummary,
    LegalActionsView,
    PhaseDeadlineFrame,
    SeatObservationFrame,
    SeatStreamFrame
  } from '$lib/api/types';

  let gameId = $derived($page.params.gameId);

  let session = $state<PlaySession | null>(null);
  let composition = $state<CompositionSummary | null>(null);
  let themePackId = $state<string | null>(null);
  let spriteByKey = $state<Record<string, string>>({});

  // Seat-scoped observation (legal actions) + transport-only phase deadline.
  let legal = $state<LegalActionsView | null>(null);
  let mySeatId = $state<string | null>(null);
  let deadlineIso = $state<string | null>(null);
  let nowMs = $state<number>(Date.now());

  let chatText = $state('');
  let composerStatus = $state<ComposerStatus>('idle');
  let actionBusy = $state(false);
  let actionNote = $state<string | null>(null);
  let error = $state<string | null>(null);
  let helpOpen = $state(false);
  let eliminationDismissed = $state(false);
  let mobilePanel = $state<'board' | 'chat' | 'actions'>('board');

  let obsSse: EventSource | null = null;
  let tickTimer: ReturnType<typeof setInterval> | null = null;

  const secondsRemaining = $derived(secondsUntil(deadlineIso, nowMs));
  const bucket = $derived(countdownBucket(secondsRemaining));
  const voteTarget = $state<{ value: string | null }>({ value: null });
  let pendingVoteTarget = $state<string | null | undefined>(undefined);
  const nightTarget = $state<{ value: string | null }>({ value: null });

  // Seat board: derived from the released frame stream (identity-blind). The
  // sprite KEY is resolved server-side to a role-agnostic archetype; here we
  // only map a stable key per seat for a deterministic placeholder render.
  const board = $derived(session?.seats ?? []);
  const chat = $derived(session?.chat ?? []);
  const phase = $derived(session?.phase ?? '—');
  const phaseBanner = $derived(session?.phaseBanner ?? null);
  const terminal = $derived(session?.terminal ?? false);
  const winner = $derived(session?.winner ?? null);
  const selectedNightActionType = $derived(nightActionType(legal));
  const selectedNightActionLabel = $derived(
    selectedNightActionType ? actionTypeLabel(selectedNightActionType) : 'Night action'
  );
  const selectedNightActionDescription = $derived(
    actionTypeDescription(legal, selectedNightActionType)
  );
  const voteTally = $derived(deriveVoteTally(session?.votes ?? {}));
  const showVoteTally = $derived(isDayVotePhaseId(phase));
  const mySeat = $derived(
    mySeatId ? (board.find((seat) => seat.public_player_id === mySeatId) ?? null) : null
  );
  const isEliminated = $derived(mySeat ? !mySeat.alive : false);

  function isDayVotePhaseId(value: string): boolean {
    const normalized = value.toUpperCase();
    return normalized.startsWith('DAY_') && normalized.endsWith('_VOTE');
  }

  function seatSpriteUrl(publicPlayerId: string): string {
    return spriteUrl(padrino.baseUrl, themePackId, spriteByKey[publicPlayerId] ?? null);
  }

  async function loadComposition(): Promise<void> {
    if (!gameId) return;
    try {
      padrino.setHumanSession(true);
      const c = await padrino.client.publicGameComposition(gameId);
      composition = c.composition;
    } catch {
      // Composition is best-effort header data; keep the board usable without it.
    }
  }

  function handleObservationFrame(frame: SeatStreamFrame): void {
    if (frame.type === 'observation') {
      const obs = frame as SeatObservationFrame;
      if (obs.legal_actions) legal = obs.legal_actions;
      const you = obs['you'] as { player_id?: string } | undefined;
      if (you?.player_id) mySeatId = you.player_id;
    } else if (frame.type === 'phase_deadline') {
      const dl = frame as PhaseDeadlineFrame;
      deadlineIso = dl.deadline_at;
    }
  }

  function connectObservation(): void {
    if (!gameId) return;
    obsSse?.close();
    const url = padrino.client.seatObservationUrl(gameId);
    obsSse = new EventSource(url, { withCredentials: true });
    obsSse.onmessage = (evt: MessageEvent) => {
      try {
        const frame = JSON.parse(evt.data as string) as SeatStreamFrame;
        handleObservationFrame(frame);
      } catch {
        // ignore malformed frames
      }
    };
    obsSse.onerror = () => {
      // The observation stream is a per-snapshot half-open stream; on error we
      // simply re-fetch the snapshot on the next phase tick rather than spin.
      obsSse?.close();
    };
  }

  function reviewVote(): void {
    actionNote = null;
    error = null;
    pendingVoteTarget = voteTarget.value;
  }

  function cancelVote(): void {
    pendingVoteTarget = undefined;
  }

  async function submitVote(): Promise<void> {
    if (!gameId) return;
    if (pendingVoteTarget === undefined) return;
    actionBusy = true;
    actionNote = null;
    error = null;
    try {
      const target = pendingVoteTarget;
      await padrino.client.submitAction(gameId, {
        action: target ? { type: 'VOTE', target } : { type: 'ABSTAIN' },
        idempotency_key: newIdempotencyKey()
      });
      pendingVoteTarget = undefined;
      actionNote = target ? 'Vote accepted.' : 'Abstain accepted.';
    } catch (e) {
      error = (e as Error).message;
    } finally {
      actionBusy = false;
    }
  }

  async function submitNightAction(): Promise<void> {
    if (!gameId) return;
    const type = nightActionType(legal);
    if (type === null) return;
    actionBusy = true;
    actionNote = null;
    error = null;
    try {
      const target = nightTarget.value;
      await padrino.client.submitAction(gameId, {
        action: target ? { type, target } : { type: 'NOOP' },
        idempotency_key: newIdempotencyKey()
      });
      actionNote = `${actionTypeLabel(type)} accepted.`;
    } catch (e) {
      error = (e as Error).message;
    } finally {
      actionBusy = false;
    }
  }

  async function sendChat(): Promise<void> {
    if (!gameId) return;
    const text = chatText.trim();
    if (text === '') return;
    composerStatus = 'pending';
    error = null;
    try {
      const result = await padrino.client.submitChat(gameId, {
        channel: 'PUBLIC',
        text,
        idempotency_key: newIdempotencyKey()
      });
      composerStatus = composerStatusFromChat(result.status);
      // A released message arrives back over the live-tail feed; clear the box.
      chatText = '';
    } catch (e) {
      composerStatus = 'error';
      error = (e as Error).message;
    }
  }

  onMount(() => {
    if (!gameId) return;
    padrino.setHumanSession(true);
    session = createPlaySession({ client: padrino.client, gameId });
    session.start();
    void loadComposition();
    connectObservation();
    tickTimer = setInterval(() => {
      nowMs = Date.now();
    }, 1000);
  });

  onDestroy(() => {
    session?.close();
    obsSse?.close();
    if (tickTimer) clearInterval(tickTimer);
  });
</script>

<div class="mb-4">
  <a class="text-sm underline" href="/">← Home</a>
</div>

<div class="mb-2 flex flex-wrap items-center gap-3">
  <div class="flex flex-wrap items-center gap-3">
    <h1 class="text-xl font-semibold" data-testid="play-title">Play</h1>
    <span
      class="rounded bg-muted px-2 py-0.5 font-mono text-xs text-muted-foreground"
      data-testid="play-phase"
    >
      {phase}
    </span>
    <span
      class="rounded bg-muted px-2 py-0.5 font-mono text-xs text-muted-foreground"
      data-testid="play-countdown"
      data-bucket={bucket}
    >
      {countdownLabel(bucket)}
    </span>
  </div>
  <Button variant="outline" testid="play-help-open" onclick={() => (helpOpen = true)}>
    Rules
  </Button>
</div>

{#if helpOpen}
  <div
    class="fixed inset-0 z-50 overflow-y-auto bg-background/95"
    data-testid="play-help-drawer"
    role="dialog"
    aria-modal="true"
    aria-labelledby="how-to-play-title"
  >
    <div
      class="ml-auto flex h-full w-full max-w-md flex-col gap-3 overflow-y-auto border-l border-border bg-background p-4 shadow-lg sm:p-5"
    >
      <div class="flex items-center justify-between gap-3">
        <p class="text-sm font-semibold">Rules</p>
        <Button variant="ghost" testid="play-help-close" onclick={() => (helpOpen = false)}>
          Close
        </Button>
      </div>
      <HowToPlayPanel class="border-0 p-0 shadow-none sm:p-0" />
    </div>
  </div>
{/if}

{#if isEliminated && !eliminationDismissed}
  <div
    class="fixed inset-0 z-40 flex items-center justify-center bg-background/85 p-4"
    data-testid="play-eliminated-modal"
    role="dialog"
    aria-modal="true"
    aria-labelledby="play-eliminated-title"
  >
    <div class="w-full max-w-sm rounded-md border border-border bg-card p-4 shadow-lg">
      <h2 class="text-base font-semibold" id="play-eliminated-title">You have been eliminated</h2>
      <p class="mt-2 text-sm text-muted-foreground">
        You can keep watching the table, but your seat can no longer submit actions.
      </p>
      <Button
        class="mt-4 min-h-11"
        variant="outline"
        testid="play-eliminated-dismiss"
        onclick={() => (eliminationDismissed = true)}
      >
        Continue watching
      </Button>
    </div>
  </div>
{/if}

<p class="mb-4 font-mono text-xs text-muted-foreground" data-testid="play-composition">
  {#if composition}
    {composition.human_count} humans · {composition.ai_count} AI · {composition.total} seats
  {:else}
    —
  {/if}
</p>

{#if phaseBanner}
  <div
    class={`mb-4 rounded-md border px-4 py-3 text-sm ${
      phaseBanner.kind === 'night'
        ? 'border-slate-300 bg-slate-50 text-slate-950'
        : 'border-amber-300 bg-amber-50 text-slate-950'
    }`}
    data-testid="play-phase-banner"
    data-phase={phaseBanner.phase}
    data-kind={phaseBanner.kind}
    role="status"
  >
    <span class="font-semibold">{phaseBanner.message}.</span>
    <span class="ml-2 font-mono text-xs">{phaseBanner.phase}</span>
  </div>
{/if}

{#if terminal}
  <div
    class="mb-4 rounded-md border border-emerald-300 bg-emerald-50 px-4 py-3 text-sm"
    data-testid="play-terminal"
    data-winner={String(winner ?? '')}
  >
    <span class="font-semibold">Game over.</span>
    {#if winner}Winner: <strong data-testid="play-winner">{winner}</strong>{/if}
  </div>
{/if}

<div
  class="mb-3 grid grid-cols-3 gap-2 md:hidden"
  role="tablist"
  aria-label="Play panels"
  data-testid="play-mobile-tabs"
>
  <button
    type="button"
    role="tab"
    aria-selected={mobilePanel === 'board'}
    class={`min-h-11 rounded-md border px-2 py-2 text-sm font-medium ${
      mobilePanel === 'board'
        ? 'border-primary bg-primary text-background'
        : 'border-border bg-card text-foreground'
    }`}
    data-testid="play-mobile-tab-board"
    onclick={() => (mobilePanel = 'board')}
  >
    Seats
  </button>
  <button
    type="button"
    role="tab"
    aria-selected={mobilePanel === 'chat'}
    class={`min-h-11 rounded-md border px-2 py-2 text-sm font-medium ${
      mobilePanel === 'chat'
        ? 'border-primary bg-primary text-background'
        : 'border-border bg-card text-foreground'
    }`}
    data-testid="play-mobile-tab-chat"
    onclick={() => (mobilePanel = 'chat')}
  >
    Chat
  </button>
  <button
    type="button"
    role="tab"
    aria-selected={mobilePanel === 'actions'}
    class={`min-h-11 rounded-md border px-2 py-2 text-sm font-medium ${
      mobilePanel === 'actions'
        ? 'border-primary bg-primary text-background'
        : 'border-border bg-card text-foreground'
    }`}
    data-testid="play-mobile-tab-actions"
    onclick={() => (mobilePanel = 'actions')}
  >
    Actions
  </button>
</div>

<div class="flex flex-col gap-3 md:grid md:grid-cols-[220px_1fr_300px]" data-testid="play-shell">
  <div
    class={`${mobilePanel === 'board' ? 'flex' : 'hidden'} flex-col gap-3 md:flex`}
    data-testid="play-board-panel"
  >
    <Card>
      <h2 class="mb-2 text-sm font-semibold">Seats</h2>
      {#if board.length === 0}
        <p class="text-xs text-muted-foreground" data-testid="play-seats-empty">Waiting…</p>
      {:else}
        <ul class="flex flex-col gap-1" data-testid="play-seat-grid">
          {#each board as seat (seat.public_player_id)}
            {@const isSelf = mySeatId === seat.public_player_id}
            <li
              class={'flex items-center gap-2 rounded px-1 py-0.5 text-xs ' +
                (seat.alive ? '' : 'text-muted-foreground line-through ') +
                (isSelf && !seat.alive
                  ? 'border border-red-300 bg-red-50 px-2 py-1 text-red-900'
                  : isSelf
                    ? 'border border-border bg-muted px-2 py-1'
                    : '')}
              data-testid="play-seat-row"
              data-player-id={seat.public_player_id}
              data-alive={String(seat.alive)}
              data-self={String(isSelf)}
              data-state={isSelf && !seat.alive ? 'dead-self' : seat.alive ? 'alive' : 'dead'}
            >
              <img
                class="h-5 w-5 rounded"
                alt="seat"
                src={seatSpriteUrl(seat.public_player_id)}
                data-testid="play-seat-sprite"
              />
              <span class="font-mono">{seat.public_player_id.slice(0, 8)}</span>
            </li>
          {/each}
        </ul>
      {/if}
    </Card>
  </div>

  <div
    class={`${mobilePanel === 'actions' || mobilePanel === 'chat' ? 'flex' : 'hidden'} flex-col gap-3 md:flex`}
    data-testid="play-main-panel"
  >
    <div class={`${mobilePanel === 'actions' ? 'block' : 'hidden'} md:block`} data-testid="play-action-panel">
      <Card>
        <h2 class="mb-2 text-sm font-semibold">Your move</h2>
        {#if terminal}
          <p class="text-xs text-muted-foreground" data-testid="play-action-ended">
            The game has ended.
          </p>
        {:else if isVotePhase(legal)}
          <div class="flex flex-col gap-2" data-testid="play-vote-panel">
            <label class="flex flex-col gap-1 text-xs">
              <span class="font-medium">Vote to eliminate</span>
              <select
                class="min-h-11 rounded border border-border bg-background px-3 py-2 text-sm"
                data-testid="play-vote-target"
                bind:value={voteTarget.value}
              >
                <option value={null}>Abstain</option>
                {#each legal?.legal_targets ?? [] as t (t)}
                  <option value={t}>{t.slice(0, 8)}</option>
                {/each}
              </select>
            </label>
            <Button
              class="min-h-11"
              testid="play-vote-submit"
              disabled={actionBusy}
              onclick={reviewVote}
            >
              Review vote
            </Button>
            {#if pendingVoteTarget !== undefined}
              <div
                class="rounded-md border border-border bg-muted p-3 text-sm"
                data-testid="play-vote-confirm"
                role="group"
                aria-label="Confirm vote"
              >
                <p class="text-xs text-muted-foreground">Confirm your vote before it is sent.</p>
                <p class="mt-1 font-mono text-sm" data-testid="play-vote-confirm-target">
                  {pendingVoteTarget === null ? 'Abstain' : pendingVoteTarget.slice(0, 8)}
                </p>
                <div class="mt-3 flex flex-wrap gap-2">
                  <Button
                    class="min-h-11"
                    testid="play-vote-confirm-submit"
                    disabled={actionBusy}
                    onclick={() => void submitVote()}
                  >
                    {actionBusy ? 'Submitting…' : 'Confirm'}
                  </Button>
                  <Button
                    class="min-h-11"
                    variant="outline"
                    testid="play-vote-confirm-cancel"
                    disabled={actionBusy}
                    onclick={cancelVote}
                  >
                    Cancel
                  </Button>
                </div>
              </div>
            {/if}
          </div>
        {:else if isNightActionPhase(legal)}
          <div class="flex flex-col gap-2" data-testid="play-night-panel">
            <label class="flex flex-col gap-1 text-xs">
              <span class="font-medium" data-testid="play-night-action-type">
                {selectedNightActionLabel}
              </span>
              {#if selectedNightActionDescription}
                <span
                  class="text-xs text-muted-foreground"
                  title={selectedNightActionDescription}
                  data-testid="play-night-action-description"
                >
                  {selectedNightActionDescription}
                </span>
              {/if}
              <select
                class="min-h-11 rounded border border-border bg-background px-3 py-2 text-sm"
                data-testid="play-night-target"
                bind:value={nightTarget.value}
              >
                <option value={null}>Skip</option>
                {#each legal?.legal_targets ?? [] as t (t)}
                  <option value={t}>{t.slice(0, 8)}</option>
                {/each}
              </select>
            </label>
            <Button
              class="min-h-11"
              testid="play-night-submit"
              disabled={actionBusy}
              onclick={() => void submitNightAction()}
            >
              {actionBusy ? 'Submitting…' : `Submit ${selectedNightActionLabel}`}
            </Button>
          </div>
        {:else}
          <p class="text-xs text-muted-foreground" data-testid="play-action-none">
            No action to take right now.
          </p>
        {/if}
        {#if actionNote}
          <p
            class="mt-2 rounded-md border border-emerald-300 bg-emerald-50 px-3 py-2 text-xs text-emerald-900"
            data-testid="play-action-note"
            role="status"
          >
            {actionNote}
          </p>
        {/if}
        {#if error}
          <p class="mt-2 text-xs text-red-500" data-testid="play-action-error">{error}</p>
        {/if}
      </Card>
    </div>

    <div class={`${mobilePanel === 'chat' ? 'block' : 'hidden'} md:block`} data-testid="play-chat-panel">
      <Card>
        <h2 class="mb-2 text-sm font-semibold">Chat</h2>
        {#if chat.length === 0}
          <p class="text-xs text-muted-foreground" data-testid="play-chat-empty">No chat yet.</p>
        {:else}
          <ol class="mb-3 flex flex-col gap-2" data-testid="play-chat-feed">
            {#each chat as line (line.sequence)}
              <li class="text-xs" data-testid="play-chat-line">
                <span class="font-mono text-muted-foreground">[{line.phase}]</span>
                {#if line.public_player_id}
                  <span class="mx-1 font-semibold">{line.public_player_id.slice(0, 8)}</span>
                {/if}
                <span>{line.text}</span>
              </li>
            {/each}
          </ol>
        {/if}

        <div
          class="sticky bottom-2 flex flex-col gap-2 rounded-md bg-card pt-2 md:static md:bottom-auto md:bg-transparent md:pt-0"
          data-testid="play-chat-composer"
        >
          <textarea
            class="min-h-24 rounded border border-border bg-background px-3 py-2 text-sm"
            rows="3"
            placeholder="Say something…"
            data-testid="play-chat-input"
            bind:value={chatText}
            disabled={terminal}
          ></textarea>
          <div class="flex items-center gap-2">
            <Button
              class="min-h-11"
              testid="play-chat-send"
              disabled={terminal || composerStatus === 'pending' || chatText.trim() === ''}
              onclick={() => void sendChat()}
            >
              Send
            </Button>
            <span
              class="text-xs text-muted-foreground"
              data-testid="play-chat-status"
              data-status={composerStatus}
            >
              {#if composerStatus === 'pending'}Holding for release…
              {:else if composerStatus === 'released'}Released
              {:else if composerStatus === 'blocked'}Blocked by moderation
              {:else if composerStatus === 'error'}Failed to send
              {:else}&nbsp;{/if}
            </span>
          </div>
        </div>
      </Card>
    </div>
  </div>

  <div
    class={`${mobilePanel === 'actions' ? 'flex' : 'hidden'} flex-col gap-3 md:flex`}
    data-testid="play-info-panel"
  >
    {#if showVoteTally}
      <Card>
        <h2 class="mb-2 text-sm font-semibold">Vote tally</h2>
        <div class="flex flex-col gap-3" data-testid="play-vote-tally-panel">
          <div>
            <h3 class="mb-1 text-xs font-medium text-muted-foreground">Running counts</h3>
            {#if voteTally.counts.length === 0}
              <p class="text-xs text-muted-foreground" data-testid="play-vote-counts-empty">
                No votes yet.
              </p>
            {:else}
              <ul class="flex flex-col gap-1" data-testid="play-vote-counts">
                {#each voteTally.counts as count (count.target)}
                  <li
                    class="flex items-center justify-between gap-3 rounded border border-border px-2 py-1 text-xs"
                    data-testid="play-vote-count-row"
                    data-target={count.target}
                    data-count={String(count.count)}
                  >
                    <span class="font-mono">{count.target.slice(0, 8)}</span>
                    <span class="font-semibold">{count.count}</span>
                  </li>
                {/each}
              </ul>
            {/if}
          </div>

          <div>
            <h3 class="mb-1 text-xs font-medium text-muted-foreground">Current votes</h3>
            {#if voteTally.rows.length === 0}
              <p class="text-xs text-muted-foreground" data-testid="play-vote-voters-empty">
                No submitted votes.
              </p>
            {:else}
              <ul class="flex flex-col gap-1" data-testid="play-vote-voters">
                {#each voteTally.rows as vote (vote.voter)}
                  <li
                    class="flex items-center justify-between gap-3 text-xs"
                    data-testid="play-vote-voter-row"
                    data-voter={vote.voter}
                    data-target={vote.target ?? ''}
                  >
                    <span class="font-mono">{vote.voter.slice(0, 8)}</span>
                    <span class="text-muted-foreground">to</span>
                    <span class="font-mono">
                      {vote.target === null ? 'Abstain' : vote.target.slice(0, 8)}
                    </span>
                  </li>
                {/each}
              </ul>
            {/if}
          </div>
        </div>
      </Card>
    {/if}

    <Card>
      <h2 class="mb-2 text-sm font-semibold">This game</h2>
      <p class="text-xs text-muted-foreground">
        Seats are shown identity-blind: you only ever see how many humans vs AI are present, never
        which seat is which, until the endgame reveal.
      </p>
      {#if mySeatId}
        <p class="mt-2 font-mono text-xs text-muted-foreground" data-testid="play-my-seat">
          You: {mySeatId.slice(0, 8)}
        </p>
      {/if}
    </Card>
  </div>
</div>
