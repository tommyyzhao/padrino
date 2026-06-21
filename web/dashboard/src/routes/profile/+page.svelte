<script lang="ts">
  // US-156: Minimal profile / stats page for signed-in accounts.
  //
  // /profile shows the signed-in account's deterministic play stats (US-145):
  // win rate by role, survival, voting accuracy, detection accuracy — with a
  // CASUAL framing and NO live ELO / rating / leaderboard (decision 6, v1).
  // Stats are gated to a signed-in account (a guest sees a sign-in prompt).
  import { onMount } from 'svelte';
  import Card from '$lib/components/Card.svelte';
  import { padrino } from '$lib/clientStore.svelte';
  import { PadrinoApiError } from '$lib/api/client';
  import type { GuestSummary, HumanPlayerStats } from '$lib/api/types';

  const RULESET_ID = 'mini7_v1';

  let me = $state<GuestSummary | null>(null);
  let stats = $state<HumanPlayerStats | null>(null);
  let error = $state<string | null>(null);
  let signedIn = $state(false);

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

  async function load(): Promise<void> {
    padrino.setHumanSession(true);
    try {
      me = await padrino.client.getHumanMe();
      signedIn = me.kind === 'account';
    } catch (e) {
      if (e instanceof PadrinoApiError && e.status === 401) {
        signedIn = false;
        return;
      }
      error = (e as Error).message;
      return;
    }
    if (!signedIn) return;
    try {
      stats = await padrino.client.getHumanStats(RULESET_ID);
    } catch (e) {
      error = (e as Error).message;
    }
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
    Signed in as <strong>{me.display_name}</strong>
  </p>
{/if}

{#if error}
  <p class="mb-3 text-sm text-red-500" data-testid="profile-error">{error}</p>
{/if}

{#if !signedIn}
  <Card>
    <p class="text-sm text-muted-foreground" data-testid="profile-signed-out">
      Sign in to see your play history and detection stats. Guest play is always casual — there is
      no ranking or rating.
    </p>
  </Card>
{:else if stats === null}
  <p class="text-sm text-muted-foreground" data-testid="profile-loading">Loading your stats…</p>
{:else}
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
