<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';
  import { PadrinoApiError } from '$lib/api/client';
  import Button from '$lib/components/Button.svelte';
  import Card from '$lib/components/Card.svelte';
  import { padrino } from '$lib/clientStore.svelte';
  import { canonicalTeamRulesets } from '$lib/rulesets';
  import type {
    PublicLadderEntry,
    PublicLiveGameEntry,
    PublicRecentGameEntry
  } from '$lib/api/types';

  // KPIs are sourced exclusively from the public spectator surface so the home
  // page works against a public-surface-only API (no private /games, /gauntlets).
  let liveNow = $state<number | null>(null);
  let recentCount = $state<number | null>(null);
  let topAgents = $state<PublicLadderEntry[]>([]);

  let liveGames = $state<PublicLiveGameEntry[]>([]);
  let recentGames = $state<PublicRecentGameEntry[]>([]);
  let matchState = $state<'idle' | 'checking' | 'needs_consent' | 'starting'>('idle');
  let matchError = $state<string | null>(null);
  let consenting = $state(false);

  let matchBusy = $derived(
    matchState === 'checking' || matchState === 'starting' || consenting
  );
  let matchLoadingText = $derived(
    matchState === 'checking'
      ? 'Checking your session...'
      : matchState === 'starting'
        ? 'Starting your table...'
        : consenting
          ? 'Accepting...'
          : null
  );

  function isUnauthorized(error: unknown): boolean {
    return error instanceof PadrinoApiError && error.status === 401;
  }

  async function load() {
    // Three public surfaces feed both the KPIs and the lobby sections.
    // Run them concurrently; each degrades independently on failure so a single
    // unavailable surface never blocks the page or shows a hard error.
    const [liveResult, recentResult, rulesetsResult] = await Promise.allSettled([
      padrino.client.publicLiveIndex(),
      padrino.client.publicRecentIndex({ limit: 10 }),
      padrino.client.publicRulesets()
    ]);

    if (liveResult.status === 'fulfilled') {
      liveGames = liveResult.value.items;
      liveNow = liveResult.value.total;
    }

    if (recentResult.status === 'fulfilled') {
      recentGames = recentResult.value.items;
      recentCount = recentResult.value.total_estimate;
    }

    if (rulesetsResult.status === 'fulfilled') {
      const firstCanonical = canonicalTeamRulesets(rulesetsResult.value.items)[0];
      if (firstCanonical) {
        try {
          const ladder = await padrino.client.publicLadder({
            ruleset_id: firstCanonical.ruleset_id,
            limit: 3
          });
          topAgents = ladder.entries.slice(0, 3);
        } catch {
          topAgents = [];
        }
      }
    }
  }

  async function ensureHumanSession(): Promise<void> {
    padrino.setHumanSession(true);
    try {
      await padrino.client.getHumanMe();
    } catch (error) {
      if (!isUnauthorized(error)) {
        throw error;
      }
      await padrino.client.createGuest();
    }
  }

  async function launchSoloMatch(): Promise<void> {
    matchState = 'starting';
    const match = await padrino.client.match();
    await goto(`/play/${encodeURIComponent(match.game_id)}`);
  }

  async function startSoloMatch(): Promise<void> {
    matchError = null;
    matchState = 'checking';
    try {
      await ensureHumanSession();
      const consent = await padrino.client.getConsentStatus();
      if (!consent.consented) {
        matchState = 'needs_consent';
        return;
      }
      await launchSoloMatch();
    } catch (error) {
      matchError = (error as Error).message;
      matchState = 'idle';
    }
  }

  async function acceptConsentAndMatch(): Promise<void> {
    consenting = true;
    matchError = null;
    try {
      const consent = await padrino.client.postConsent();
      if (!consent.consented) {
        matchState = 'needs_consent';
        matchError = 'Consent is required before play.';
        return;
      }
      await launchSoloMatch();
    } catch (error) {
      matchError = (error as Error).message;
      matchState = 'needs_consent';
    } finally {
      consenting = false;
    }
  }

  onMount(load);
</script>

<section
  id="play-vs-ai"
  class="mb-8 grid gap-4 border-b border-border pb-6 lg:grid-cols-[1fr_auto] lg:items-center"
  data-testid="home-entry"
>
  <div>
    <p class="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
      Casual anonymous Mafia
    </p>
    <h1 class="text-2xl font-semibold tracking-normal">Play vs AI</h1>
    <p class="mt-2 max-w-2xl text-sm text-muted-foreground">
      Start a private table with one human seat and curated AI fill. Identities stay hidden until
      the reveal.
    </p>
    <div class="mt-4 flex flex-wrap items-center gap-3">
      <Button
        class="px-5 py-2.5"
        testid="home-play-vs-ai-cta"
        onclick={() => void startSoloMatch()}
        disabled={matchBusy}
      >
        {matchBusy ? 'Preparing...' : 'Play vs AI'}
      </Button>
      <a
        class="text-sm underline-offset-2 hover:underline"
        href="#lobby-live-section"
        data-testid="home-watch-link"
      >
        Watch live games
      </a>
    </div>

    {#if matchLoadingText}
      <p class="mt-3 text-sm text-muted-foreground" data-testid="home-match-loading">
        {matchLoadingText}
      </p>
    {/if}

    {#if matchState === 'needs_consent'}
      <Card class="mt-4 max-w-xl border-amber-400" data-testid="home-consent-card">
        <p class="mb-3 flex items-start gap-2 text-xs" data-testid="home-consent-row">
          <span>
            I accept the <strong>Terms</strong> and <strong>Privacy Policy</strong> and confirm I am
            <strong>16 or older</strong>.
          </span>
        </p>
        <Button
          testid="home-consent-accept"
          onclick={() => void acceptConsentAndMatch()}
          disabled={consenting}
        >
          {consenting ? 'Accepting...' : 'Accept & play'}
        </Button>
      </Card>
    {/if}

    {#if matchError}
      <p class="mt-3 text-sm text-red-500" data-testid="home-match-error">{matchError}</p>
    {/if}
  </div>
</section>

<div class="grid gap-4 sm:grid-cols-3" data-testid="home-kpis">
  <Card>
    <div class="text-xs uppercase tracking-wider text-muted-foreground">Live now</div>
    <div class="mt-1 text-3xl font-semibold" data-testid="home-kpi-live-now">
      {liveNow ?? '—'}
    </div>
  </Card>
  <Card>
    <div class="text-xs uppercase tracking-wider text-muted-foreground">Recent games</div>
    <div class="mt-1 text-3xl font-semibold" data-testid="home-kpi-recent-games">
      {recentCount ?? '—'}
    </div>
  </Card>
  <Card>
    <div class="text-xs uppercase tracking-wider text-muted-foreground">Top agent</div>
    <div class="mt-1 text-xl font-semibold" data-testid="home-kpi-top-agent">
      {topAgents[0]?.display_name ?? '—'}
    </div>
  </Card>
</div>

<section class="mt-8" data-testid="home-top-agents">
  <div class="mb-3 flex items-center justify-between gap-3">
    <h2 class="text-lg font-semibold">Top 3 agents</h2>
    <a
      href="/leaderboard"
      class="text-sm underline-offset-2 hover:underline"
      data-testid="home-leaderboard-link"
    >
      Leaderboard
    </a>
  </div>
  {#if topAgents.length === 0}
    <p class="text-sm text-muted-foreground" data-testid="home-top-agents-empty">
      No ranked results yet.
    </p>
  {:else}
    <Card>
      <table class="w-full text-sm" data-testid="home-top-agents-table">
        <thead class="text-left text-xs uppercase tracking-wider text-muted-foreground">
          <tr>
            <th class="pb-2">Rank</th>
            <th class="pb-2">Agent</th>
            <th class="pb-2 text-right">Ordinal</th>
            <th class="pb-2 text-right">Games</th>
          </tr>
        </thead>
        <tbody>
          {#each topAgents as agent, i (agent.agent_build_id)}
            <tr class="border-t border-border" data-testid="home-top-agent-row">
              <td class="py-2">{i + 1}</td>
              <td class="py-2 font-medium">
                <a
                  href="/ladder"
                  class="underline-offset-2 hover:underline"
                  data-testid="home-top-agent-link"
                >
                  {agent.display_name}
                </a>
              </td>
              <td class="py-2 text-right">{agent.ordinal}</td>
              <td class="py-2 text-right">{agent.games}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </Card>
  {/if}
</section>

<section class="mt-8" data-testid="lobby-live-section">
  <h2 class="mb-3 text-lg font-semibold">Live Now</h2>
  {#if liveGames.length === 0}
    <p class="text-sm text-muted-foreground" data-testid="lobby-live-empty">No games live right now.</p>
  {:else}
    <ul class="grid gap-3 sm:grid-cols-2" data-testid="lobby-live-list">
      {#each liveGames as game (game.game_id)}
        <li>
          <a href="/watch/{game.game_id}" class="block" data-testid="lobby-live-card">
            <Card>
              <div class="font-mono text-xs text-muted-foreground mb-1">{game.game_id.slice(0, 8)}</div>
              <div class="font-medium" data-testid="lobby-live-ruleset">{game.ruleset_id}</div>
              {#if game.current_phase}
                <div class="mt-1 text-sm text-muted-foreground" data-testid="lobby-live-phase">{game.current_phase}</div>
              {/if}
              <div class="mt-1 text-sm" data-testid="lobby-live-players">{game.players_alive} alive</div>
              <span class="mt-2 inline-block rounded bg-emerald-500/20 px-2 py-0.5 text-xs text-emerald-600">Live</span>
            </Card>
          </a>
        </li>
      {/each}
    </ul>
  {/if}
</section>

<section class="mt-8" data-testid="lobby-recent-section">
  <h2 class="mb-3 text-lg font-semibold">Recently Finished</h2>
  {#if recentGames.length === 0}
    <p class="text-sm text-muted-foreground" data-testid="lobby-recent-empty">No recent games.</p>
  {:else}
    <ul class="grid gap-3 sm:grid-cols-2" data-testid="lobby-recent-list">
      {#each recentGames as game (game.game_id)}
        <li>
          <a href="/watch/{game.game_id}" class="block" data-testid="lobby-recent-card">
            <Card>
              <div class="font-mono text-xs text-muted-foreground mb-1">{game.game_id.slice(0, 8)}</div>
              <div class="font-medium" data-testid="lobby-recent-ruleset">{game.ruleset_id}</div>
              {#if game.terminal_result}
                <div class="mt-1 text-sm font-semibold" data-testid="lobby-recent-winner">
                  Winner: {String(game.terminal_result.winner)}
                </div>
              {/if}
            </Card>
          </a>
        </li>
      {/each}
    </ul>
  {/if}
</section>
