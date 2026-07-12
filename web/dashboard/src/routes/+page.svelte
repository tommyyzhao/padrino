<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';
  import { PadrinoApiError } from '$lib/api/client';
  import type {
    FundingPreflightResponse,
    LegalDocumentResponse,
    OpenRouterConnectionStatus,
    PromotionalWallet,
    PublicLadderEntry,
    PublicLiveGameEntry,
    PublicRecentGameEntry
  } from '$lib/api/types';
  import Button from '$lib/components/Button.svelte';
  import Card from '$lib/components/Card.svelte';
  import Dialog from '$lib/components/Dialog.svelte';
  import HowToPlayPanel from '$lib/components/HowToPlayPanel.svelte';
  import StatusBadge from '$lib/components/StatusBadge.svelte';
  import { padrino } from '$lib/clientStore.svelte';
  import { dismissHowToPlay, hasDismissedHowToPlay } from '$lib/howToPlay';
  import { canonicalTeamRulesets } from '$lib/rulesets';

  type BaseConsentKind = 'TOS' | 'PRIVACY' | 'AGE_GATE';
  type FundingChoice = 'platform' | 'byok';
  type PublicSurfaceState = 'loading' | 'ready' | 'offline';
  type QuickPlayState = 'idle' | 'checking' | 'funding' | 'quoting' | 'quoted' | 'launching';

  let liveNow = $state<number | null>(null);
  let recentCount = $state<number | null>(null);
  let topAgents = $state<PublicLadderEntry[]>([]);
  let liveGames = $state<PublicLiveGameEntry[]>([]);
  let recentGames = $state<PublicRecentGameEntry[]>([]);
  let publicSurfaceState = $state<PublicSurfaceState>('loading');

  let consentRequired = $state(false);
  let consenting = $state(false);
  let consentError = $state<string | null>(null);
  let consentDocuments = $state<Partial<Record<BaseConsentKind, LegalDocumentResponse>>>({});
  let showHowToPlay = $state(false);

  let showQuickPlay = $state(false);
  let quickState = $state<QuickPlayState>('idle');
  let quickError = $state<string | null>(null);
  let quickWallet = $state<PromotionalWallet | null>(null);
  let quickConnection = $state<OpenRouterConnectionStatus | null>(null);
  let fundingChoice = $state<FundingChoice | null>(null);
  let quickQuote = $state<FundingPreflightResponse | null>(null);
  let quickLobbyId = $state<string | null>(null);
  let byokMaximum = $state('5.00');

  let baseConsentHashes = $derived(
    consentDocuments.TOS && consentDocuments.PRIVACY && consentDocuments.AGE_GATE
      ? {
          TOS: consentDocuments.TOS.content_hash,
          PRIVACY: consentDocuments.PRIVACY.content_hash,
          AGE_GATE: consentDocuments.AGE_GATE.content_hash
        }
      : null
  );
  let baseConsentVersionLabel = $derived(
    consentDocuments.TOS && consentDocuments.PRIVACY
      ? `Terms ${consentDocuments.TOS.version_label} / Privacy ${consentDocuments.PRIVACY.version_label}`
      : null
  );
  let promotionalFundingAvailable = $derived((quickWallet?.available_micros ?? 0) > 0);
  let byokFundingAvailable = $derived(quickConnection?.connected === true);
  let quickBusy = $derived(quickState === 'checking' || quickState === 'quoting' || quickState === 'launching');
  let quoteCanLaunch = $derived(
    quickQuote?.allowed === true &&
      quickQuote.funding_quote_hash !== null &&
      quickLobbyId !== null
  );

  function isUnauthorized(error: unknown): boolean {
    return error instanceof PadrinoApiError && error.status === 401;
  }

  function detailToken(error: unknown): string {
    if (!(error instanceof PadrinoApiError)) return '';
    const detail = error.detail;
    if (typeof detail === 'string') return detail;
    if (detail && typeof detail === 'object' && 'detail' in detail) {
      const nested = (detail as { detail?: unknown }).detail;
      return typeof nested === 'string' ? nested : '';
    }
    return '';
  }

  function friendlyFailure(error: unknown, fallback: string): string {
    const token = detailToken(error).toLowerCase();
    if (token.includes('insufficient') || token.includes('promotional_credit')) {
      return 'Your promotional credit cannot cover this table. Connect OpenRouter or adjust a custom table.';
    }
    if (token.includes('openrouter') && token.includes('connection')) {
      return 'Your OpenRouter connection is not ready. Reconnect it, then request a fresh estimate.';
    }
    if (token.includes('catalog') || token.includes('autofill')) {
      return 'The model cabinet is refreshing. Try again shortly or configure a custom table.';
    }
    if (token.includes('quote') || token.includes('funding')) {
      return 'That estimate is no longer current. Request a fresh bound estimate before launching.';
    }
    if (error instanceof TypeError) {
      return 'The laboratory is offline. Check your connection, then try again.';
    }
    return fallback;
  }

  function formatMicros(micros: number | null): string {
    if (micros === null) return 'Estimate pending';
    const sign = micros < 0 ? '-' : '';
    const absolute = Math.abs(micros);
    const whole = Math.floor(absolute / 1_000_000);
    const cents = Math.floor((absolute % 1_000_000) / 10_000)
      .toString()
      .padStart(2, '0');
    return `${sign}$${whole}.${cents}`;
  }

  function microsFromUsd(value: string): number | null {
    const trimmed = value.trim();
    if (!/^\d+(?:\.\d{1,6})?$/.test(trimmed)) return null;
    const [wholePart, fractionalPart = ''] = trimmed.split('.');
    const whole = Number(wholePart);
    if (!Number.isSafeInteger(whole)) return null;
    const fraction = Number(fractionalPart.padEnd(6, '0'));
    const micros = whole * 1_000_000 + fraction;
    return Number.isSafeInteger(micros) ? micros : null;
  }

  async function load(): Promise<void> {
    publicSurfaceState = 'loading';
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
    publicSurfaceState =
      liveResult.status === 'rejected' && recentResult.status === 'rejected'
        ? 'offline'
        : 'ready';
  }

  async function ensureHumanSession(): Promise<void> {
    padrino.setHumanSession(true);
    try {
      await padrino.client.getHumanMe();
    } catch (error) {
      if (!isUnauthorized(error)) throw error;
      await padrino.client.createGuest();
    }
  }

  async function loadBaseConsentDocuments(): Promise<void> {
    const [tos, privacy, age] = await Promise.all([
      padrino.client.legalDocument('TOS'),
      padrino.client.legalDocument('PRIVACY'),
      padrino.client.legalDocument('AGE_GATE')
    ]);
    consentDocuments = { TOS: tos, PRIVACY: privacy, AGE_GATE: age };
  }

  async function promptForBaseConsent(): Promise<void> {
    consentRequired = true;
    consentError = null;
    try {
      await loadBaseConsentDocuments();
    } catch (error) {
      consentError = friendlyFailure(error, 'Consent details are temporarily unavailable. Please try again.');
    }
  }

  async function openQuickPlayFunding(): Promise<void> {
    showQuickPlay = true;
    quickState = 'checking';
    quickError = null;
    quickQuote = null;
    quickLobbyId = null;
    try {
      await ensureHumanSession();
      const consent = await padrino.client.getConsentStatus();
      if (!consent.consented) {
        showQuickPlay = false;
        quickState = 'idle';
        await promptForBaseConsent();
        return;
      }
      const [walletResult, connectionResult] = await Promise.allSettled([
        padrino.client.getWallet(),
        padrino.client.getOpenRouterConnectionStatus()
      ]);
      quickWallet = walletResult.status === 'fulfilled' ? walletResult.value : null;
      quickConnection = connectionResult.status === 'fulfilled' ? connectionResult.value : null;
      fundingChoice = promotionalFundingAvailable
        ? 'platform'
        : byokFundingAvailable
          ? 'byok'
          : null;
      quickState = 'funding';
      if (walletResult.status === 'rejected' && connectionResult.status === 'rejected') {
        quickError = 'Funding status is unavailable right now. Reconnect and try again.';
      }
    } catch (error) {
      quickState = 'idle';
      showQuickPlay = false;
      quickError = friendlyFailure(error, 'We could not prepare Quick Play. Please try again.');
    }
  }

  async function beginQuickPlay(): Promise<void> {
    consentRequired = false;
    consentError = null;
    if (!hasDismissedHowToPlay()) {
      showHowToPlay = true;
      return;
    }
    await openQuickPlayFunding();
  }

  async function acceptConsentAndPrepareQuickPlay(): Promise<void> {
    consenting = true;
    consentError = null;
    try {
      if (baseConsentHashes === null) await loadBaseConsentDocuments();
      if (baseConsentHashes === null) {
        consentError = 'Consent documents are still loading.';
        return;
      }
      const consent = await padrino.client.postConsent({ content_hashes: baseConsentHashes });
      if (!consent.consented) {
        consentError = 'Consent is required before play.';
        return;
      }
      consentRequired = false;
      await openQuickPlayFunding();
    } catch (error) {
      consentError = friendlyFailure(error, 'We could not record your consent. Please try again.');
    } finally {
      consenting = false;
    }
  }

  function selectFunding(source: FundingChoice): void {
    fundingChoice = source;
    quickQuote = null;
    quickLobbyId = null;
    quickError = null;
    quickState = 'funding';
  }

  async function requestBoundQuote(): Promise<void> {
    if (fundingChoice === null) {
      quickError = 'Choose promotional credit or a connected OpenRouter account first.';
      return;
    }
    const byokMaximumMicros = fundingChoice === 'byok' ? microsFromUsd(byokMaximum) : null;
    if (fundingChoice === 'byok' && (byokMaximumMicros === null || byokMaximumMicros <= 0)) {
      quickError = 'Enter a whole-game maximum spend, such as 5.00.';
      return;
    }

    quickState = 'quoting';
    quickError = null;
    quickQuote = null;
    quickLobbyId = null;
    try {
      const lobby = await padrino.client.createLobby({
        built_in_ruleset_id: 'mini7_v1',
        cadence_preset: 'casual_standard_v1',
        human_seat_count: 1,
        identity_mode: 'ANONYMOUS',
        autofill_policy: { funding_source: fundingChoice }
      });
      const quote = await padrino.client.fundingPreflight(
        lobby.id,
        fundingChoice === 'byok' ? { byok_max_estimate_micros: byokMaximumMicros } : {}
      );
      quickLobbyId = lobby.id;
      quickQuote = quote;
      quickState = 'quoted';
      if (!quote.allowed) {
        quickError = quote.reason.includes('credit')
          ? 'Your promotional credit cannot cover this table. Choose a connected OpenRouter account or try a custom table.'
          : 'This table cannot be funded with the selected source right now. Choose another source or try again.';
      }
    } catch (error) {
      quickState = 'funding';
      quickError = friendlyFailure(error, 'We could not prepare a bound estimate. Try again.');
    }
  }

  async function launchQuickPlay(): Promise<void> {
    const quote = quickQuote;
    const lobbyId = quickLobbyId;
    if (!quoteCanLaunch || quote === null || quote.funding_quote_hash === null || lobbyId === null) return;
    const byokMaximumMicros = fundingChoice === 'byok' ? microsFromUsd(byokMaximum) : null;
    quickState = 'launching';
    quickError = null;
    try {
      await padrino.client.setLobbyReady(lobbyId, true);
      await padrino.client.lockLobby(lobbyId);
      const launched = await padrino.client.launchFundedLobby(lobbyId, {
        funding_quote_hash: quote.funding_quote_hash,
        byok_max_estimate_micros: byokMaximumMicros
      });
      showQuickPlay = false;
      await goto(`/play/${encodeURIComponent(launched.game_id)}`);
    } catch (error) {
      quickState = 'quoted';
      quickError = friendlyFailure(error, 'The table is no longer ready. Request a fresh estimate and try again.');
    }
  }

  function closeQuickPlay(): void {
    showQuickPlay = false;
    quickState = 'idle';
  }

  function continueAfterHowToPlay(): void {
    dismissHowToPlay();
    showHowToPlay = false;
    void openQuickPlayFunding();
  }

  function closeHowToPlay(): void {
    showHowToPlay = false;
  }

  onMount(() => {
    void load();
    if (new URLSearchParams(window.location.search).get('consent') === 'required') {
      padrino.setHumanSession(true);
      void promptForBaseConsent();
    }
  });
</script>

<section class="relative overflow-hidden rounded-xl border border-border bg-card px-5 py-8 shadow-sm sm:px-8 sm:py-10">
  <div class="pointer-events-none absolute -right-28 -top-24 h-64 w-64 rounded-full bg-primary/10 blur-3xl"></div>
  <p class="padrino-eyebrow">Occult laboratory · casual play</p>
  <div class="relative mt-3 max-w-3xl">
    <h1 class="text-3xl font-semibold tracking-tight sm:text-4xl">Choose your table.</h1>
    <p class="mt-3 max-w-2xl text-sm leading-6 text-muted-foreground sm:text-base">
      Play one paced game of social deduction with people and models. Your public seat stays
      anonymous until the final reveal.
    </p>
  </div>
</section>

<section id="game-mode-home" class="mt-6 grid gap-4 lg:grid-cols-3" data-testid="game-mode-home" aria-label="Game modes">
  <Card class="flex flex-col" data-testid="mode-card-quick-play">
    <div class="flex items-start justify-between gap-3">
      <div>
        <p class="padrino-eyebrow">Immediate table</p>
        <h2 class="mt-2 text-xl font-semibold">Quick Play</h2>
      </div>
      <StatusBadge status="live" label="Casual" />
    </div>
    <p class="mt-3 text-sm leading-6 text-muted-foreground">
      One player, an anonymous server-curated AI roster, and a complete game in about 20–30
      minutes.
    </p>
    <dl class="mt-5 grid gap-3 text-sm">
      <div><dt class="text-xs uppercase tracking-wider text-muted-foreground">Audience</dt><dd>Solo explorers</dd></div>
      <div><dt class="text-xs uppercase tracking-wider text-muted-foreground">Rating</dt><dd>No ranked rating</dd></div>
      <div><dt class="text-xs uppercase tracking-wider text-muted-foreground">Funding</dt><dd>Promo credit or your connected OpenRouter account</dd></div>
      <div><dt class="text-xs uppercase tracking-wider text-muted-foreground">Configuration</dt><dd>The laboratory selects a balanced casual table</dd></div>
    </dl>
    <Button class="mt-6 w-full" testid="home-play-vs-ai-cta" onclick={() => void beginQuickPlay()}>
      Enter Quick Play
    </Button>
  </Card>

  <Card class="flex flex-col" data-testid="mode-card-custom-lobby">
    <div class="flex items-start justify-between gap-3">
      <div>
        <p class="padrino-eyebrow">Private laboratory</p>
        <h2 class="mt-2 text-xl font-semibold">Custom Lobby</h2>
      </div>
      <StatusBadge status="neutral" label="Friends" />
    </div>
    <p class="mt-3 text-sm leading-6 text-muted-foreground">
      Invite friends, build a supported table, and run a 20–30 minute private experiment.
    </p>
    <dl class="mt-5 grid gap-3 text-sm">
      <div><dt class="text-xs uppercase tracking-wider text-muted-foreground">Audience</dt><dd>Hosts and invited friends</dd></div>
      <div><dt class="text-xs uppercase tracking-wider text-muted-foreground">Rating</dt><dd>Always casual — never ranked</dd></div>
      <div><dt class="text-xs uppercase tracking-wider text-muted-foreground">Funding</dt><dd>Host chooses promo credit or connected OpenRouter</dd></div>
      <div><dt class="text-xs uppercase tracking-wider text-muted-foreground">Configuration</dt><dd>Roles, models, cadence, theme, and identity mode</dd></div>
    </dl>
    <a class="padrino-button padrino-button--outline mt-6 flex min-h-10 items-center justify-center" href="/lobby" data-testid="mode-card-custom-lobby-cta">
      Build a casual table
    </a>
  </Card>

  <Card class="flex flex-col border-violet-400/40" data-testid="mode-card-ranked">
    <div class="flex items-start justify-between gap-3">
      <div>
        <p class="padrino-eyebrow">Seasonal queue</p>
        <h2 class="mt-2 text-xl font-semibold">Ranked</h2>
      </div>
      <StatusBadge status="warning" label="Coming next" />
    </div>
    <p class="mt-3 text-sm leading-6 text-muted-foreground">
      Ranked Solo and Mixed arrive after their season, calibration, and safety gates are ready.
    </p>
    <dl class="mt-5 grid gap-3 text-sm">
      <div><dt class="text-xs uppercase tracking-wider text-muted-foreground">Audience</dt><dd>Verified solo entrants</dd></div>
      <div><dt class="text-xs uppercase tracking-wider text-muted-foreground">Rating</dt><dd>Season-scoped, server-run</dd></div>
      <div><dt class="text-xs uppercase tracking-wider text-muted-foreground">Funding</dt><dd>Server-owned when the season opens</dd></div>
      <div><dt class="text-xs uppercase tracking-wider text-muted-foreground">Configuration</dt><dd>No opponent, role, or model selection</dd></div>
    </dl>
    <Button class="mt-6 w-full" testid="mode-card-ranked-cta" disabled>Ranked coming next</Button>
  </Card>
</section>

<section class="mt-6 flex flex-wrap items-center gap-3" aria-label="Game orientation">
  <Button variant="outline" testid="home-onboarding-reopen" onclick={() => (showHowToPlay = true)}>
    Review the 2-minute briefing
  </Button>
  <a class="text-sm underline-offset-2 hover:underline" href="/how-to-play" data-testid="home-how-to-play-link">
    Open the full rules guide
  </a>
</section>

{#if consentRequired}
  <Card class="mt-6 max-w-2xl border-amber-400/60" data-testid="home-consent-card">
    <p class="mb-3 flex items-start gap-2 text-sm" data-testid="home-consent-row">
      <span>
        Before you enter a table, accept the
        <a class="font-semibold underline" href="/terms" data-testid="home-consent-terms-link">Terms</a>
        and
        <a class="font-semibold underline" href="/privacy" data-testid="home-consent-privacy-link">Privacy Policy</a>,
        and confirm you are <strong>16 or older</strong>.
      </span>
    </p>
    {#if baseConsentVersionLabel}
      <p class="mb-3 text-xs text-muted-foreground" data-testid="home-consent-version">{baseConsentVersionLabel}</p>
    {/if}
    <Button testid="home-consent-accept" onclick={() => void acceptConsentAndPrepareQuickPlay()} disabled={consenting || baseConsentHashes === null}>
      {consenting ? 'Accepting…' : 'Accept and continue'}
    </Button>
    {#if consentError}
      <p class="mt-3 text-sm text-red-300" data-testid="home-consent-error">{consentError}</p>
    {/if}
  </Card>
{/if}

{#if showHowToPlay}
  <div class="fixed inset-0 z-50 overflow-y-auto bg-background/85 px-4 py-6 backdrop-blur-sm" data-testid="how-to-play-modal" role="dialog" aria-modal="true" aria-labelledby="how-to-play-title">
    <div class="mx-auto max-w-3xl">
      <HowToPlayPanel>
        <Button testid="how-to-play-continue" onclick={continueAfterHowToPlay}>Got it, choose funding</Button>
        <Button variant="ghost" testid="how-to-play-back" onclick={closeHowToPlay}>Back</Button>
        <a class="text-sm underline-offset-2 hover:underline" href="/how-to-play">Open full guide</a>
      </HowToPlayPanel>
    </div>
  </div>
{/if}

<Dialog id="quick-play" title="Quick Play funding" description="Choose one funding source, review a bound whole-game estimate, then enter the table." open={showQuickPlay} onclose={closeQuickPlay}>
  {#if quickState === 'checking'}
    <div class="py-6 text-sm text-muted-foreground" data-testid="quick-play-loading">Checking your table and funding options…</div>
  {:else}
    <div class="grid gap-3" data-testid="quick-play-funding">
      <p class="text-sm text-muted-foreground">Quick Play is casual and anonymous. This choice funds the entire AI roster; it never enables Ranked.</p>

      <label class="rounded-lg border border-border p-4 transition-colors has-[:checked]:border-primary has-[:checked]:bg-primary/5" data-testid="quick-funding-promo">
        <span class="flex items-start justify-between gap-3">
          <span>
            <span class="block font-semibold">Platform promotional credit</span>
            <span class="mt-1 block text-sm text-muted-foreground">Available balance: {formatMicros(quickWallet?.available_micros ?? null)}</span>
          </span>
          <input type="radio" name="quick-funding" checked={fundingChoice === 'platform'} disabled={!promotionalFundingAvailable || quickBusy} onchange={() => selectFunding('platform')} aria-label="Use promotional credit" />
        </span>
        {#if !promotionalFundingAvailable}
          <span class="mt-2 block text-xs text-amber-200" data-testid="quick-promo-exhausted">No usable promotional credit is available for this table.</span>
        {/if}
      </label>

      <label class="rounded-lg border border-border p-4 transition-colors has-[:checked]:border-primary has-[:checked]:bg-primary/5" data-testid="quick-funding-byok">
        <span class="flex items-start justify-between gap-3">
          <span>
            <span class="block font-semibold">Connected OpenRouter account</span>
            <span class="mt-1 block text-sm text-muted-foreground">{quickConnection?.connected ? `Connected${quickConnection.display_suffix ? ` · ${quickConnection.display_suffix}` : ''}` : 'Connect an account to use your own OpenRouter funding.'}</span>
          </span>
          <input type="radio" name="quick-funding" checked={fundingChoice === 'byok'} disabled={!byokFundingAvailable || quickBusy} onchange={() => selectFunding('byok')} aria-label="Use connected OpenRouter" />
        </span>
        {#if !byokFundingAvailable}
          <a class="mt-2 inline-block text-xs font-semibold underline" href="/profile">Connect OpenRouter in your profile</a>
        {/if}
      </label>

      {#if fundingChoice === 'byok'}
        <label class="grid gap-1 text-sm" for="quick-byok-maximum">
          <span class="font-medium">Maximum whole-game spend (USD)</span>
          <input id="quick-byok-maximum" class="rounded border border-border bg-background px-3 py-2" data-testid="quick-byok-maximum" inputmode="decimal" bind:value={byokMaximum} disabled={quickBusy} />
          <span class="text-xs text-muted-foreground">You will confirm this ceiling before the game is created.</span>
        </label>
      {/if}

      {#if quickQuote}
        <Card class={quickQuote.allowed ? 'border-emerald-400/60' : 'border-amber-400/60'} data-testid="quick-bound-quote">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p class="padrino-eyebrow">Bound estimate</p>
              <p class="mt-1 text-lg font-semibold">{formatMicros(quickQuote.expected_total_micros)}</p>
              <p class="mt-1 text-xs text-muted-foreground">Frozen for this casual table until its configuration changes.</p>
            </div>
            <StatusBadge status={quickQuote.allowed ? 'live' : 'warning'} label={quickQuote.allowed ? 'Ready to launch' : 'Needs attention'} />
          </div>
        </Card>
      {/if}

      {#if quickError}
        <p class="rounded-md border border-red-400/40 bg-red-950/30 px-3 py-2 text-sm text-red-200" data-testid="quick-play-error">{quickError}</p>
      {/if}

      <div class="flex flex-wrap justify-end gap-3 pt-2">
        <Button variant="outline" onclick={closeQuickPlay} disabled={quickBusy}>Not now</Button>
        {#if quoteCanLaunch}
          <Button testid="quick-launch" onclick={() => void launchQuickPlay()} disabled={quickBusy}>{quickState === 'launching' ? 'Opening table…' : 'Enter table'}</Button>
        {:else}
          <Button testid="quick-get-quote" onclick={() => void requestBoundQuote()} disabled={fundingChoice === null || quickBusy}>{quickState === 'quoting' ? 'Binding estimate…' : 'Get bound estimate'}</Button>
        {/if}
      </div>
    </div>
  {/if}
</Dialog>

<section class="mt-10" data-testid="home-kpis">
  <div class="mb-3 flex items-end justify-between gap-4">
    <div>
      <p class="padrino-eyebrow">Spectator observatory</p>
      <h2 class="mt-1 text-xl font-semibold">The laboratory is in motion</h2>
    </div>
    <a class="text-sm underline-offset-2 hover:underline" href="/games" data-testid="home-watch-link">Observe games</a>
  </div>

  {#if publicSurfaceState === 'offline'}
    <Card class="border-amber-400/60" data-testid="home-public-offline">
      <p class="font-semibold">The observatory signal is unavailable.</p>
      <p class="mt-1 text-sm text-muted-foreground">Your play options remain here; reconnect when you want live experiments and the ladder.</p>
      <Button class="mt-4" variant="outline" onclick={() => void load()}>Reconnect observatory</Button>
    </Card>
  {:else}
    <div class="grid gap-4 sm:grid-cols-3">
      <Card>
        <div class="text-xs uppercase tracking-wider text-muted-foreground">Live now</div>
        <div class="mt-1 text-3xl font-semibold" data-testid="home-kpi-live-now">{liveNow ?? 'Loading signal'}</div>
      </Card>
      <Card>
        <div class="text-xs uppercase tracking-wider text-muted-foreground">Recent games</div>
        <div class="mt-1 text-3xl font-semibold" data-testid="home-kpi-recent-games">{recentCount ?? 'Loading signal'}</div>
      </Card>
      <Card>
        <div class="text-xs uppercase tracking-wider text-muted-foreground">Leading agent</div>
        <div class="mt-1 text-xl font-semibold" data-testid="home-kpi-top-agent">{topAgents[0]?.display_name ?? 'Calibrating'}</div>
      </Card>
    </div>

    <div class="mt-8 grid gap-8 lg:grid-cols-2">
      <section data-testid="home-top-agents">
        <div class="mb-3 flex items-center justify-between gap-3">
          <h2 class="text-lg font-semibold">Top agents</h2>
          <a href="/leaderboard" class="text-sm underline-offset-2 hover:underline" data-testid="home-leaderboard-link">Leaderboard</a>
        </div>
        {#if topAgents.length === 0}
          <Card><p class="text-sm text-muted-foreground" data-testid="home-top-agents-empty">The ladder is calibrating. Check back after the next completed experiment.</p></Card>
        {:else}
          <Card>
            <table class="w-full text-sm" data-testid="home-top-agents-table">
              <thead class="text-left text-xs uppercase tracking-wider text-muted-foreground"><tr><th class="pb-2">Rank</th><th class="pb-2">Agent</th><th class="pb-2 text-right">Ordinal</th><th class="pb-2 text-right">Games</th></tr></thead>
              <tbody>
                {#each topAgents as agent, i (agent.agent_build_id)}
                  <tr class="border-t border-border" data-testid="home-top-agent-row"><td class="py-2">{i + 1}</td><td class="py-2 font-medium"><a href="/ladder" class="underline-offset-2 hover:underline" data-testid="home-top-agent-link">{agent.display_name}</a></td><td class="py-2 text-right">{agent.ordinal}</td><td class="py-2 text-right">{agent.games}</td></tr>
                {/each}
              </tbody>
            </table>
          </Card>
        {/if}
      </section>

      <section data-testid="lobby-live-section">
        <h2 class="mb-3 text-lg font-semibold">Live now</h2>
        {#if liveGames.length === 0}
          <Card><p class="text-sm text-muted-foreground" data-testid="lobby-live-empty">No public experiment is streaming at this moment. Start a casual table or return when the next signal appears.</p></Card>
        {:else}
          <ul class="grid gap-3 sm:grid-cols-2" data-testid="lobby-live-list">
            {#each liveGames as game (game.game_id)}
              <li><a href="/watch/{game.game_id}" class="block" data-testid="lobby-live-card"><Card><div class="font-mono text-xs text-muted-foreground mb-1">{game.game_id.slice(0, 8)}</div><div class="font-medium" data-testid="lobby-live-ruleset">{game.ruleset_id}</div>{#if game.current_phase}<div class="mt-1 text-sm text-muted-foreground" data-testid="lobby-live-phase">{game.current_phase}</div>{/if}<div class="mt-1 text-sm" data-testid="lobby-live-players">{game.players_alive} alive</div></Card></a></li>
            {/each}
          </ul>
        {/if}
      </section>
    </div>

    <section class="mt-8" data-testid="lobby-recent-section">
      <h2 class="mb-3 text-lg font-semibold">Recently finished</h2>
      {#if recentGames.length === 0}
        <Card><p class="text-sm text-muted-foreground" data-testid="lobby-recent-empty">The archive is waiting for its next completed table.</p></Card>
      {:else}
        <ul class="grid gap-3 sm:grid-cols-2" data-testid="lobby-recent-list">
          {#each recentGames as game (game.game_id)}
            <li><a href="/watch/{game.game_id}" class="block" data-testid="lobby-recent-card"><Card><div class="font-mono text-xs text-muted-foreground mb-1">{game.game_id.slice(0, 8)}</div><div class="font-medium" data-testid="lobby-recent-ruleset">{game.ruleset_id}</div>{#if game.terminal_result}<div class="mt-1 text-sm font-semibold" data-testid="lobby-recent-winner">Winner: {String(game.terminal_result.winner)}</div>{/if}</Card></a></li>
          {/each}
        </ul>
      {/if}
    </section>
  {/if}
</section>
