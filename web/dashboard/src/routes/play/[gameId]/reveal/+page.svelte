<script lang="ts">
  // US-156: Endgame reveal + spot-the-AI guess.
  //
  // /play/[gameId]/reveal is the post-terminal surface. The spot-the-AI guess
  // UI is shown FIRST and GATES the disclosure of the viewer's own detection
  // accuracy AND the canonical endgame reveal: the imitation-game hook is that
  // you commit your guess before you learn the truth. After a single submit the
  // backend returns the viewer's accuracy and the per-seat reveal (human/AI,
  // role, faction, exact model, takeover provenance, themed sprite) is shown.
  //
  // The guess, accuracy, and private-game reveal are cookie-authenticated
  // human-session calls. The reveal is the ONLY surface that opens identity
  // (AGENTS.md rule 7), gated on terminal game state server-side.
  import { page } from '$app/stores';
  import { onMount } from 'svelte';
  import Card from '$lib/components/Card.svelte';
  import Button from '$lib/components/Button.svelte';
  import { padrino } from '$lib/clientStore.svelte';
  import { spriteUrl } from '$lib/api/playSurface';
  import { revealSpriteKey } from '$lib/api/reveal';
  import { PadrinoApiError } from '$lib/api/client';
  import type {
    EndgameReveal,
    SeatGuess,
    SeatObservationFrame,
    SeatReveal,
    SeatStreamFrame,
    TuringGuessResult
  } from '$lib/api/types';

  let gameId = $derived($page.params.gameId);

  let reveal = $state<EndgameReveal | null>(null);
  let accuracy = $state<TuringGuessResult | null>(null);
  let guesses = $state<Record<string, SeatGuess>>({});
  let submitting = $state(false);
  let error = $state<string | null>(null);
  let themePackId = $state<string | null>(null);
  let mySeatId = $state<string | null>(null);

  // The guess gates everything: until the viewer has submitted (or already
  // submitted in a prior session), the reveal board + accuracy stay hidden.
  const guessed = $derived(accuracy !== null);

  // Every seat except the viewer's own is guessable. The reveal board orders by
  // seat_index for a stable render.
  const seats = $derived(
    reveal ? [...reveal.seats].sort((a, b) => a.seat_index - b.seat_index) : []
  );
  // Exclude the viewer's own seat: resolved from the observation snapshot
  // pre-guess, then confirmed by the scored result's guesser_public_id.
  const ownSeatId = $derived(accuracy?.guesser_public_id ?? mySeatId);
  const guessableSeats = $derived(
    reveal ? seats.filter((s) => s.public_player_id !== ownSeatId) : []
  );

  function setGuess(playerId: string, value: SeatGuess): void {
    guesses = { ...guesses, [playerId]: value };
  }

  function seatSpriteUrl(seat: SeatReveal): string {
    return spriteUrl(padrino.baseUrl, themePackId, revealSpriteKey(seat));
  }

  async function loadReveal(): Promise<void> {
    if (!gameId) return;
    try {
      reveal = await padrino.client.humanGameReveal(gameId);
    } catch (e) {
      error = (e as Error).message;
    }
  }

  async function loadExistingGuess(): Promise<void> {
    if (!gameId) return;
    try {
      accuracy = await padrino.client.getTuringGuess(gameId);
    } catch (e) {
      // A 404 means the viewer has not guessed yet — that is the normal
      // pre-guess state, not an error.
      if (e instanceof PadrinoApiError && e.status === 404) return;
      // A 401 means no human session; the guess UI is still shown but submit
      // will surface the auth error. Keep the page usable.
    }
  }

  function resolveMySeat(): void {
    if (!gameId) return;
    // The viewer's own seat is read from a single observation snapshot frame so
    // the guess UI can exclude it (a guesser never guesses their own seat).
    const sse = new EventSource(padrino.client.seatObservationUrl(gameId), {
      withCredentials: true
    });
    sse.onmessage = (evt: MessageEvent) => {
      try {
        const frame = JSON.parse(evt.data as string) as SeatStreamFrame;
        if (frame.type === 'observation') {
          const you = (frame as SeatObservationFrame)['you'] as { player_id?: string } | undefined;
          if (you?.player_id) {
            mySeatId = you.player_id;
            sse.close();
          }
        }
      } catch {
        // ignore malformed frames
      }
    };
    sse.onerror = () => sse.close();
  }

  async function submitGuess(): Promise<void> {
    if (!gameId || reveal === null) return;
    submitting = true;
    error = null;
    try {
      // Send the viewer's toggled guesses. The guesser's own seat is excluded
      // server-side, so an untoggled own seat simply never appears here; the
      // backend scores against the seats it owns the truth for.
      accuracy = await padrino.client.submitTuringGuess(gameId, { ...guesses });
    } catch (e) {
      error = (e as Error).message;
    } finally {
      submitting = false;
    }
  }

  onMount(() => {
    if (!gameId) return;
    padrino.setHumanSession(true);
    void loadReveal();
    void loadExistingGuess();
    resolveMySeat();
  });
</script>

<div class="mb-4">
  <a class="text-sm underline" href="/">← Home</a>
</div>

<h1 class="mb-3 text-xl font-semibold" data-testid="reveal-title">Endgame reveal</h1>

{#if error}
  <p class="mb-3 text-sm text-red-500" data-testid="reveal-error">{error}</p>
{/if}

{#if reveal === null}
  <p class="text-sm text-muted-foreground" data-testid="reveal-loading">Loading reveal…</p>
{:else if !guessed}
  <!-- Spot-the-AI guess UI: gates the reveal + accuracy. -->
  <Card>
    <div data-testid="guess-panel">
      <h2 class="mb-1 text-sm font-semibold">Spot the AI</h2>
      <p class="mb-3 text-xs text-muted-foreground">
        Before you see the truth, guess which seats were AI. Commit your guess to reveal everyone —
        and your own detection accuracy.
      </p>
      <ul class="mb-3 flex flex-col gap-2">
        {#each guessableSeats as seat (seat.public_player_id)}
          <li
            class="flex items-center gap-3 rounded border border-border px-2 py-1 text-sm"
            data-testid="guess-seat-row"
            data-player-id={seat.public_player_id}
          >
            <img
              class="h-6 w-6 rounded"
              alt="seat"
              src={seatSpriteUrl(seat)}
              data-testid="guess-seat-sprite"
            />
            <span class="font-mono">{seat.public_player_id.slice(0, 8)}</span>
            <span class="ml-auto flex gap-1">
              <button
                type="button"
                class={'rounded px-2 py-0.5 text-xs ' +
                  (guesses[seat.public_player_id] === 'HUMAN'
                    ? 'bg-sky-600 text-white'
                    : 'bg-muted text-muted-foreground')}
                data-testid={`guess-${seat.public_player_id}-HUMAN`}
                data-selected={String(guesses[seat.public_player_id] === 'HUMAN')}
                onclick={() => setGuess(seat.public_player_id, 'HUMAN')}
              >
                Human
              </button>
              <button
                type="button"
                class={'rounded px-2 py-0.5 text-xs ' +
                  (guesses[seat.public_player_id] === 'AI'
                    ? 'bg-violet-600 text-white'
                    : 'bg-muted text-muted-foreground')}
                data-testid={`guess-${seat.public_player_id}-AI`}
                data-selected={String(guesses[seat.public_player_id] === 'AI')}
                onclick={() => setGuess(seat.public_player_id, 'AI')}
              >
                AI
              </button>
            </span>
          </li>
        {/each}
      </ul>
      <Button testid="guess-submit" disabled={submitting} onclick={() => void submitGuess()}>
        {submitting ? 'Submitting…' : 'Reveal everyone'}
      </Button>
    </div>
  </Card>
{:else}
  <!-- Accuracy (gated behind the guess) + the full per-seat reveal. -->
  {#if accuracy}
    <div
      class="mb-4 rounded-md border border-sky-300 bg-sky-50 px-4 py-3 text-sm"
      data-testid="reveal-accuracy"
    >
      <span class="font-semibold">Your detection:</span>
      You correctly identified
      <strong>{accuracy.correct}</strong>
      of
      <strong>{accuracy.total}</strong>
      seats (accuracy
      <span data-testid="reveal-accuracy-value">{accuracy.accuracy}</span>).
    </div>
  {/if}

  <Card>
    <div data-testid="reveal-board">
      <div class="mb-2 flex items-center gap-2">
        <h2 class="text-sm font-semibold">The truth</h2>
        {#if reveal.winner}
          <span
            class="rounded bg-muted px-2 py-0.5 font-mono text-xs text-muted-foreground"
            data-testid="reveal-winner"
          >
            {reveal.winner} wins
          </span>
        {/if}
      </div>
      <ul class="flex flex-col gap-2">
        {#each seats as seat (seat.public_player_id)}
          <li
            class={'flex items-center gap-3 rounded border border-border px-2 py-1 text-sm ' +
              (seat.alive ? '' : 'opacity-70')}
            data-testid="reveal-seat-row"
            data-player-id={seat.public_player_id}
          >
            <img
              class="h-7 w-7 rounded"
              alt="seat"
              src={seatSpriteUrl(seat)}
              data-testid="reveal-seat-sprite"
            />
            <span class="font-mono">{seat.public_player_id.slice(0, 8)}</span>
            <span
              class={'rounded px-2 py-0.5 text-xs ' +
                (seat.is_human ? 'bg-sky-100 text-sky-800' : 'bg-violet-100 text-violet-800')}
              data-testid="reveal-seat-kind"
            >
              {seat.is_human ? 'Human' : 'AI'}
            </span>
            <span class="font-mono text-xs text-muted-foreground" data-testid="reveal-seat-role">
              {seat.role} · {seat.faction}
            </span>
            {#if seat.takeover_provenance !== 'HUMAN' && seat.takeover_provenance !== 'AI'}
              <span
                class="rounded bg-amber-100 px-1.5 py-0.5 font-mono text-[10px] text-amber-800"
                data-testid="reveal-seat-provenance"
              >
                {seat.takeover_provenance}{seat.taken_over_at_phase
                  ? ` @ ${seat.taken_over_at_phase}`
                  : ''}
              </span>
            {/if}
            {#if seat.model}
              <span
                class="ml-auto font-mono text-xs text-muted-foreground"
                data-testid="reveal-seat-model"
              >
                {seat.model.display_name ?? `${seat.model.provider}/${seat.model.model_name}`}
                <span class="text-[10px]">({seat.model.provider})</span>
              </span>
            {/if}
          </li>
        {/each}
      </ul>
    </div>
  </Card>
{/if}
