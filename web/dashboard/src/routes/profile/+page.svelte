<script lang="ts">
  // US-156/US-286: Minimal casual profile / stats page for human principals.
  //
  // /profile shows the authenticated human principal's deterministic play stats
  // and own completed game history (US-145/276/286):
  // win rate by role, survival, voting accuracy, detection accuracy — with a
  // CASUAL framing and NO live ELO / rating / leaderboard (decision 6, v1).
  // Guests can see their current-session stats; account sign-in is an upsell
  // for persistence, not a gate for the profile surface.
  import { onMount } from 'svelte';
  import Card from '$lib/components/Card.svelte';
  import { padrino } from '$lib/clientStore.svelte';
  import { PadrinoApiError } from '$lib/api/client';
  import type { GuestSummary, HumanGameHistoryEntry, HumanPlayerStats } from '$lib/api/types';

  const RULESET_ID = 'mini7_v1';

  let me = $state<GuestSummary | null>(null);
  let stats = $state<HumanPlayerStats | null>(null);
  let history = $state<HumanGameHistoryEntry[] | null>(null);
  let error = $state<string | null>(null);
  let historyError = $state<string | null>(null);
  let sessionChecked = $state(false);
  let signedOut = $state(false);
  let statsLoading = $state(false);
  let historyLoading = $state(false);

  const isGuest = $derived(me?.kind === 'guest');

  function pct(value: string | number): string {
    if (typeof value === 'number') {
      return Number.isNaN(value) ? '—' : `${Math.round(value * 100)}%`;
    }
    // The core emits some accuracy ratios as an exact 'num/den' fraction
    // string (core/turing/scoring.py _accuracy_string), which Number() turns
    // into NaN. Parse the ratio so '2/3' renders a percentage, not an em-dash.
    const fraction = /^(-?\d+)\/(\d+)$/.exec(value.trim());
    if (fraction) {
      const den = Number(fraction[2]);
      if (den !== 0) return `${Math.round((Number(fraction[1]) / den) * 100)}%`;
    }
    const n = Number(value);
    if (!Number.isNaN(n)) return `${Math.round(n * 100)}%`;
    // Fall back to rendering the core string verbatim rather than an em-dash.
    return value.trim() === '' ? '—' : value;
  }

  function dateLabel(value: string): string {
    return value.slice(0, 10);
  }

  function spotLabel(item: HumanGameHistoryEntry): string {
    if (item.spot_the_ai === null) return 'Not guessed yet';
    return `${item.spot_the_ai.correct}/${item.spot_the_ai.total} (${pct(item.spot_the_ai.accuracy)})`;
  }

  async function load(): Promise<void> {
    padrino.setHumanSession(true);
    sessionChecked = false;
    signedOut = false;
    stats = null;
    history = null;
    error = null;
    historyError = null;
    try {
      me = await padrino.client.getHumanMe();
    } catch (e) {
      if (e instanceof PadrinoApiError && e.status === 401) {
        signedOut = true;
        sessionChecked = true;
        return;
      }
      error = (e as Error).message;
      sessionChecked = true;
      return;
    }
    sessionChecked = true;
    statsLoading = true;
    historyLoading = true;
    const [statsResult, historyResult] = await Promise.allSettled([
      padrino.client.getHumanStats(RULESET_ID),
      padrino.client.listHumanGames({ limit: 20 })
    ]);
    if (statsResult.status === 'fulfilled') {
      stats = statsResult.value;
    } else {
      error = (statsResult.reason as Error).message;
    }
    if (historyResult.status === 'fulfilled') {
      history = historyResult.value.items;
    } else {
      historyError = (historyResult.reason as Error).message;
    }
    statsLoading = false;
    historyLoading = false;
  }

  onMount(() => {
    void load();
  });
</script>

<div class="mb-4">
  <a class="text-sm underline" href="/">← Home</a>
</div>

<h1 class="mb-1 text-xl font-semibold" data-testid="profile-title">Your profile</h1>
{#if me?.display_name}
  <p class="mb-3 text-sm text-muted-foreground" data-testid="profile-display-name">
    Playing as <strong>{me.display_name}</strong>
  </p>
{/if}

{#if sessionChecked && isGuest && !signedOut}
  <p class="mb-3 text-sm text-muted-foreground" data-testid="profile-guest-upsell">
    Sign in to save your history beyond this guest session.
  </p>
{/if}

{#if error}
  <p class="mb-3 text-sm text-red-500" data-testid="profile-error">{error}</p>
{/if}

{#if !sessionChecked}
  <p class="text-sm text-muted-foreground" data-testid="profile-loading">Loading your profile…</p>
{:else if signedOut}
  <Card>
    <p class="text-sm text-muted-foreground" data-testid="profile-signed-out">
      Sign in to see your play history and detection stats. Guest play is always casual — there is
      no ranking or rating.
    </p>
  </Card>
{:else if statsLoading}
  <p class="text-sm text-muted-foreground" data-testid="profile-loading">Loading your stats…</p>
{:else if stats !== null}
  <Card>
    <div data-testid="profile-stats">
      <p class="mb-3 text-xs text-muted-foreground">
        Casual play history — for fun, not ranked. No standings here.
      </p>
      <dl class="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <div>
          <dt class="text-xs text-muted-foreground">Games</dt>
          <dd class="font-mono" data-testid="profile-games">{stats.games}</dd>
        </div>
        <div>
          <dt class="text-xs text-muted-foreground">Wins</dt>
          <dd class="font-mono" data-testid="profile-wins">{stats.wins}</dd>
        </div>
        <div>
          <dt class="text-xs text-muted-foreground">Survival</dt>
          <dd class="font-mono" data-testid="profile-survival">{pct(stats.survival_rate)}</dd>
        </div>
        <div>
          <dt class="text-xs text-muted-foreground">Spot-the-AI</dt>
          <dd class="font-mono" data-testid="profile-detection-accuracy">
            {pct(stats.detection_accuracy)}
          </dd>
        </div>
      </dl>

      <div class="mt-4">
        <h2 class="mb-1 text-xs font-semibold text-muted-foreground">Voting accuracy</h2>
        <p class="font-mono text-sm" data-testid="profile-voting-accuracy">
          {stats.voting_accuracy.accurate_votes}/{stats.voting_accuracy.total_votes}
          ({pct(stats.voting_accuracy.rate)})
        </p>
      </div>

      {#if stats.role_win_rates.length > 0}
        <div class="mt-4">
          <h2 class="mb-1 text-xs font-semibold text-muted-foreground">Win rate by role</h2>
          <ul class="flex flex-col gap-1" data-testid="profile-role-win-rates">
            {#each stats.role_win_rates as r (r.role)}
              <li class="font-mono text-sm" data-testid="profile-role-win-rate">
                {r.role}: {r.wins}/{r.games} ({pct(r.rate)})
              </li>
            {/each}
          </ul>
        </div>
      {/if}
    </div>
  </Card>
{/if}

{#if sessionChecked && !signedOut}
  <Card class="mt-4">
    <section data-testid="profile-history">
      <div class="mb-3 flex items-center justify-between gap-3">
        <h2 class="text-sm font-semibold">Match history</h2>
        {#if history !== null}
          <span class="font-mono text-xs text-muted-foreground">{history.length} recent</span>
        {/if}
      </div>

      {#if historyLoading}
        <p class="text-sm text-muted-foreground" data-testid="profile-history-loading">
          Loading your games…
        </p>
      {:else if historyError}
        <p class="text-sm text-red-500" data-testid="profile-history-error">
          Could not load match history. Your stats above are still available.
        </p>
      {:else if history !== null && history.length > 0}
        <ul class="flex flex-col gap-2">
          {#each history as game (game.game_id)}
            <li
              class="rounded border border-border px-3 py-2 text-sm"
              data-testid="profile-history-row"
            >
              <div class="flex flex-wrap items-center gap-2">
                <span class="font-mono text-xs text-muted-foreground">
                  {dateLabel(game.ended_at)}
                </span>
                <span
                  class="rounded bg-muted px-2 py-0.5 font-mono text-xs"
                  data-testid="profile-history-result"
                >
                  {game.result}
                </span>
                <span class="font-mono text-xs">{game.ruleset_id}</span>
                <a
                  class="ml-auto text-xs underline"
                  href={game.reveal_path}
                  data-testid="profile-history-link"
                >
                  Reveal
                </a>
              </div>
              <div class="mt-2 grid gap-1 text-xs text-muted-foreground sm:grid-cols-2">
                <p>
                  Role
                  <span class="font-mono text-foreground">{game.role}</span>
                </p>
                <p>
                  Spot-the-AI
                  <span class="font-mono text-foreground">{spotLabel(game)}</span>
                </p>
              </div>
            </li>
          {/each}
        </ul>
      {:else}
        <div class="rounded border border-dashed border-border px-3 py-4" data-testid="profile-empty-history">
          <p class="mb-2 text-sm text-muted-foreground">No games yet.</p>
          <a class="text-sm font-semibold underline" href="/" data-testid="profile-empty-play">
            Play vs AI
          </a>
        </div>
      {/if}
    </section>
  </Card>
{/if}
