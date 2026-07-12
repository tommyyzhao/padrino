<script lang="ts">
  // US-154: Lobby detail — invite share, consent + 16+ gate, count-only roster,
  // ready / host controls, start (launch). Identity-blind: the roster never
  // exposes a per-seat human/AI map, only counts (AGENTS.md rule 7).
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import { onMount, onDestroy } from 'svelte';
  import Card from '$lib/components/Card.svelte';
  import Button from '$lib/components/Button.svelte';
  import { PadrinoApiError } from '$lib/api/client';
  import { padrino } from '$lib/clientStore.svelte';
  import type {
    LobbyLaunchReadiness,
    LobbyRoster,
    LobbySummary,
    RosterMember
  } from '$lib/api/types';

  interface LobbyStateFrame {
    type: 'lobby_state';
    lobby_id: string;
    status: string;
    composition: LobbyRoster['composition'];
    members: RosterMember[];
    launch_readiness?: LobbyLaunchReadiness;
  }

  type FundingQuoteState = 'checking' | 'current' | 'missing' | 'stale' | 'unavailable';

  let lobbyId = $derived($page.params.id);
  // The table builder passes this opaque confirmation through the waiting room.
  // It is never a credential and is supplied to the server only at launch.
  let fundingQuoteHash = $derived($page.url.searchParams.get('funding_quote_hash'));
  let fundingSource = $derived($page.url.searchParams.get('funding_source'));
  let byokMaximumMicros = $derived.by(() => {
    const raw = $page.url.searchParams.get('byok_max_estimate_micros');
    if (raw === null || !/^\d+$/.test(raw)) return null;
    const parsed = Number(raw);
    return Number.isSafeInteger(parsed) ? parsed : null;
  });

  let summary = $state<LobbySummary | null>(null);
  let roster = $state<LobbyRoster | null>(null);
  let consented = $state(false);
  let displayName = $state('');

  let loading = $state(true);
  let error = $state<string | null>(null);
  let consenting = $state(false);
  let readyBusy = $state(false);
  let launching = $state(false);
  let copied = $state(false);
  let memberActionBusy = $state<string | null>(null);
  let fundingQuoteState = $state<FundingQuoteState>('checking');
  let streamState = $state<'connecting' | 'live' | 'reconnecting'>('connecting');

  let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  let lobbyStream: EventSource | null = null;

  // The host can launch; non-hosts only ready up. The roster tells each caller
  // which row is their own without exposing principal ids or game seats.
  let isHost = $derived(roster?.members.some((m) => m.is_self && m.host) ?? false);
  let iAmReady = $derived(roster?.members.find((member) => member.is_self)?.ready ?? false);
  let serverReadiness = $derived(
    roster?.launch_readiness ?? { allowed: false, reasons: ['room_loading'] }
  );
  let launchReadinessMessages = $derived(
    serverReadiness.reasons.map((reason) => readinessMessage(reason))
  );
  let canStart = $derived(
    isHost &&
      roster?.status === 'OPEN' &&
      serverReadiness.allowed &&
      fundingQuoteState === 'current' &&
      !launching
  );

  const inviteUrl = $derived(
    summary === null
      ? ''
      : typeof window === 'undefined'
        ? summary.invite_token
        : `${window.location.origin}/lobby?invite=${encodeURIComponent(summary.invite_token)}`
  );

  async function loadAll(): Promise<void> {
    if (!lobbyId) return;
    try {
      padrino.setHumanSession(true);
      const [s, r, c, me] = await Promise.all([
        padrino.client.getLobby(lobbyId),
        padrino.client.getLobbyRoster(lobbyId),
        padrino.client.getConsentStatus(),
        padrino.client.getHumanMe()
      ]);
      summary = s;
      roster = r;
      consented = c.consented;
      if (me.display_name) displayName = me.display_name;
      error = null;
      startLobbyStateStream();
      if (r.members.some((member) => member.is_self && member.host)) {
        void verifyFundingQuote();
      }
    } catch (e) {
      error = lobbyErrorMessage(e);
    } finally {
      loading = false;
    }
  }

  async function sendHeartbeat(): Promise<void> {
    if (!lobbyId) return;
    try {
      // Heartbeat keeps our presence alive and returns the current roster.
      roster = await padrino.client.lobbyHeartbeat(lobbyId);
    } catch {
      // The state stream reconnects independently; keep the last good roster.
    }
  }

  function startLobbyStateStream(): void {
    if (!lobbyId || typeof EventSource === 'undefined') return;
    lobbyStream?.close();
    streamState = 'connecting';
    const stream = new EventSource(padrino.client.lobbyStateUrl(lobbyId), { withCredentials: true });
    lobbyStream = stream;
    stream.onopen = () => {
      streamState = 'live';
    };
    stream.onmessage = (event: MessageEvent<string>) => {
      const frame = parseLobbyStateFrame(event.data);
      if (frame === null || frame.lobby_id !== lobbyId) return;
      roster = {
        id: lobbyId,
        status: frame.status,
        member_count: frame.members.length,
        composition: frame.composition,
        members: frame.members,
        launch_readiness: frame.launch_readiness ?? roster?.launch_readiness
      };
      // The state event is the update trigger. This one direct reconciliation
      // read also evicts a member whose presence just became stale.
      void refreshRosterAfterStateEvent();
      if (frame.members.some((member) => member.is_self && member.host)) {
        void verifyFundingQuote();
      }
    };
    stream.onerror = () => {
      streamState = 'reconnecting';
    };
  }

  async function refreshRosterAfterStateEvent(): Promise<void> {
    if (!lobbyId) return;
    try {
      roster = await padrino.client.getLobbyRoster(lobbyId);
    } catch {
      // EventSource retries automatically; preserve its safe last snapshot.
    }
  }

  async function acceptConsent(): Promise<void> {
    consenting = true;
    error = null;
    try {
      if (displayName.trim() !== '') {
        await padrino.client.setHumanDisplayName(displayName.trim());
      }
      const status = await padrino.client.postConsent();
      consented = status.consented;
    } catch (e) {
      error = lobbyErrorMessage(e);
    } finally {
      consenting = false;
    }
  }

  async function toggleReady(next: boolean): Promise<void> {
    if (!lobbyId) return;
    readyBusy = true;
    try {
      roster = await padrino.client.setLobbyReady(lobbyId, next);
      if (isHost) void verifyFundingQuote();
    } catch (e) {
      error = lobbyErrorMessage(e);
    } finally {
      readyBusy = false;
    }
  }

  async function startGame(): Promise<void> {
    if (!lobbyId) return;
    const quoteHash = fundingQuoteHash;
    if (!canStart) {
      error = launchReadinessMessages[0] ?? 'This table is not ready to start yet.';
      return;
    }
    if (quoteHash === null) {
      error = 'Return to the table builder and refresh the funding quote before starting.';
      return;
    }
    if (!(await verifyFundingQuote())) {
      return;
    }
    launching = true;
    error = null;
    try {
      await padrino.client.lockLobby(lobbyId);
      const launched = await padrino.client.launchFundedLobby(lobbyId, {
        funding_quote_hash: quoteHash,
        byok_max_estimate_micros: fundingSource === 'byok' ? byokMaximumMicros : null
      });
      await goto(`/play/${encodeURIComponent(launched.game_id)}`);
    } catch (e) {
      error = lobbyErrorMessage(e);
    } finally {
      launching = false;
    }
  }

  async function copyInvite(): Promise<void> {
    try {
      await navigator.clipboard.writeText(inviteUrl);
      copied = true;
      setTimeout(() => (copied = false), 1500);
    } catch {
      // clipboard may be blocked; the link text remains selectable
    }
  }

  async function shareInvite(): Promise<void> {
    if (typeof navigator.share !== 'function') {
      await copyInvite();
      return;
    }
    try {
      await navigator.share({ title: 'Join my Padrino table', text: 'Join my casual table.', url: inviteUrl });
    } catch {
      // Share sheets are cancelable; the copy button remains an equivalent path.
    }
  }

  async function leaveLobby(): Promise<void> {
    if (!lobbyId) return;
    memberActionBusy = 'leave';
    error = null;
    try {
      await padrino.client.leaveLobby(lobbyId);
      await goto('/lobby');
    } catch (e) {
      error = lobbyErrorMessage(e);
    } finally {
      memberActionBusy = null;
    }
  }

  async function kickMember(member: RosterMember): Promise<void> {
    if (!lobbyId) return;
    memberActionBusy = `kick:${member.member_id}`;
    error = null;
    try {
      summary = await padrino.client.kickLobbyMember(lobbyId, member.member_id);
      await refreshRosterAfterStateEvent();
    } catch (e) {
      error = lobbyErrorMessage(e);
    } finally {
      memberActionBusy = null;
    }
  }

  async function transferHost(member: RosterMember): Promise<void> {
    if (!lobbyId) return;
    memberActionBusy = `transfer:${member.member_id}`;
    error = null;
    try {
      roster = await padrino.client.transferLobbyHost(lobbyId, member.member_id);
    } catch (e) {
      error = lobbyErrorMessage(e);
    } finally {
      memberActionBusy = null;
    }
  }

  async function verifyFundingQuote(): Promise<boolean> {
    if (!lobbyId || fundingQuoteHash === null) {
      fundingQuoteState = 'missing';
      return false;
    }
    if (fundingSource === 'byok' && byokMaximumMicros === null) {
      fundingQuoteState = 'missing';
      return false;
    }
    fundingQuoteState = 'checking';
    try {
      const quote = await padrino.client.fundingPreflight(
        lobbyId,
        fundingSource === 'byok' ? { byok_max_estimate_micros: byokMaximumMicros } : {}
      );
      if (!quote.allowed) {
        fundingQuoteState = 'unavailable';
        return false;
      }
      fundingQuoteState = quote.funding_quote_hash === fundingQuoteHash ? 'current' : 'stale';
      return fundingQuoteState === 'current';
    } catch {
      fundingQuoteState = 'unavailable';
      return false;
    }
  }

  function parseLobbyStateFrame(raw: string): LobbyStateFrame | null {
    try {
      const value = JSON.parse(raw) as Partial<LobbyStateFrame>;
      if (
        value.type !== 'lobby_state' ||
        typeof value.lobby_id !== 'string' ||
        typeof value.status !== 'string' ||
        !Array.isArray(value.members) ||
        value.composition === undefined
      ) {
        return null;
      }
      return value as LobbyStateFrame;
    } catch {
      return null;
    }
  }

  function readinessMessage(reason: string): string {
    const messages: Record<string, string> = {
      room_loading: 'Checking the table with the server.',
      missing_seat: 'A member needs a table place before the game can begin.',
      duplicate_seat: 'The room is being reconciled. Please wait a moment.',
      orphan_seat: 'A member needs a table place before the game can begin.',
      absent_member: 'Waiting for a friend to reconnect.',
      not_ready_member: 'Waiting for every member to ready up.',
      unresolved_ai_seat: 'Refresh the table quote after choosing the AI roster.',
      count_mismatch: 'The table configuration needs attention before launch.'
    };
    return messages[reason] ?? 'The server is not ready to begin this table yet.';
  }

  function lobbyErrorMessage(cause: unknown): string {
    if (!(cause instanceof PadrinoApiError)) return 'The room could not update. Check your connection and try again.';
    const detail = cause.detail;
    const token =
      typeof detail === 'object' && detail !== null && 'detail' in detail && typeof detail.detail === 'string'
        ? detail.detail
        : '';
    const messages: Record<string, string> = {
      host_must_transfer_before_leave: 'Choose another host before leaving this room.',
      not_host: 'Only the current host can make that room change.',
      member_not_found: 'That member has already left the room.',
      lobby_not_open: 'This room is no longer accepting that change.',
      lobby_full: 'This room is at its human invitation capacity.',
      funding_quote_stale: 'The table quote changed. Return to the builder and refresh it.',
      insufficient_promotional_credit: 'Promotional credit is no longer sufficient for this table.'
    };
    return messages[token] ?? 'The room could not update. Check its current state and try again.';
  }

  onMount(() => {
    void loadAll();
    heartbeatTimer = setInterval(() => void sendHeartbeat(), 15000);
  });

  onDestroy(() => {
    if (heartbeatTimer) clearInterval(heartbeatTimer);
    lobbyStream?.close();
  });
</script>

<div class="mb-4">
  <a class="text-sm underline" href="/lobby">← Lobbies</a>
</div>

{#if loading}
  <p class="text-sm text-muted-foreground" data-testid="lobby-loading">Loading lobby…</p>
{:else if error && summary === null}
  <p class="text-sm text-red-500" data-testid="lobby-error">{error}</p>
{:else if summary !== null}
  <div class="mb-3 flex items-center gap-3">
    <h1 class="text-xl font-semibold" data-testid="lobby-title">Lobby</h1>
    <span
      class="rounded bg-muted px-2 py-0.5 font-mono text-xs text-muted-foreground"
      data-testid="lobby-status"
    >
      {summary.status}
    </span>
    <span
      class="rounded bg-muted px-2 py-0.5 font-mono text-xs text-muted-foreground"
      data-testid="lobby-stakes"
    >
      {summary.ranked ? 'RANKED' : summary.stakes}
    </span>
  </div>

  <p class="mb-4 font-mono text-xs text-muted-foreground" data-testid="lobby-ruleset">
    {summary.ruleset_id} · {summary.identity_mode}
  </p>
  <p class="mb-4 text-sm text-cyan" data-testid="lobby-casual-notice">
    Custom room · casual only — this table does not affect ratings.
  </p>

  <div class="grid gap-4 md:grid-cols-[1fr_280px]">
    <div class="flex flex-col gap-4">
      {#if !consented}
        <Card class="border-amber-400">
          <h2 class="mb-2 text-sm font-semibold">Before you play</h2>
          <p class="mb-3 text-xs text-muted-foreground">
            Sign in to keep your stats, or just play as a guest. Either way, accept the terms below.
          </p>
          <label class="mb-3 flex flex-col gap-1 text-xs">
            <span class="font-medium">Display name (optional)</span>
            <input
              class="rounded border border-border bg-background px-2 py-1 text-sm"
              data-testid="lobby-display-name"
              bind:value={displayName}
              placeholder="how friends see you"
            />
          </label>
          <a
            class="mb-3 inline-block text-xs underline"
            href={`${padrino.baseUrl}/human/oauth/google/start`}
            data-testid="lobby-signin-cta"
          >
            Sign in to save stats
          </a>
          <p class="mb-3 flex items-start gap-2 text-xs" data-testid="lobby-consent-row">
            <span>
              I accept the <strong>Terms</strong> and <strong>Privacy Policy</strong> and confirm I
              am <strong>16 or older</strong>.
            </span>
          </p>
          <Button
            testid="lobby-consent-accept"
            onclick={() => void acceptConsent()}
            disabled={consenting}
          >
            {consenting ? 'Accepting…' : 'Accept & continue'}
          </Button>
          {#if error}
            <p class="mt-2 text-xs text-red-500" data-testid="lobby-consent-error">{error}</p>
          {/if}
        </Card>
      {:else}
        <Card data-testid="lobby-ready-card">
          <h2 class="mb-2 text-sm font-semibold">Ready up</h2>
          <p class="mb-3 text-xs text-muted-foreground">
            When everyone is ready, the host starts the game.
          </p>
          <div class="flex items-center gap-2">
            <Button
              testid="lobby-ready-toggle"
              variant={iAmReady ? 'outline' : 'default'}
              disabled={readyBusy}
              onclick={() => {
                void toggleReady(!iAmReady);
              }}
            >
              {iAmReady ? 'Not ready' : "I'm ready"}
            </Button>
            <span class="text-xs text-muted-foreground" data-testid="lobby-ready-state">
              {iAmReady ? 'Ready' : 'Not ready'}
            </span>
          </div>

          {#if isHost}
            <div class="mt-4 border-t border-border pt-3">
              <h3 class="mb-2 text-sm font-medium">Launch control</h3>
              {#if fundingQuoteState === 'missing'}
                <p class="mb-2 text-xs text-warning" data-testid="lobby-funding-quote-missing">
                  A fresh funding quote is required before this casual table can start.
                </p>
              {:else if fundingQuoteState === 'checking'}
                <p class="mb-2 text-xs text-muted-foreground" data-testid="lobby-funding-quote-checking">
                  Checking the frozen funding and model quote with the server…
                </p>
              {:else if fundingQuoteState === 'stale'}
                <p class="mb-2 text-xs text-warning" data-testid="lobby-funding-quote-stale">
                  This table changed after its quote. Return to the builder and refresh it before launch.
                </p>
              {:else if fundingQuoteState === 'unavailable'}
                <p class="mb-2 text-xs text-warning" data-testid="lobby-funding-quote-unavailable">
                  The server cannot fund this quote right now. Refresh it in the table builder.
                </p>
              {:else}
                <p class="mb-2 text-xs text-muted-foreground" data-testid="lobby-funding-quote-bound">
                  {fundingSource === 'byok'
                    ? 'Connected OpenRouter maximum is bound to this launch confirmation.'
                    : 'Promotional-credit quote is bound to this launch confirmation.'}
                </p>
              {/if}
              <Button
                testid="lobby-start"
                disabled={!canStart}
                onclick={() => void startGame()}
              >
                {launching ? 'Starting…' : 'Start game'}
              </Button>
              {#if !serverReadiness.allowed}
                <ul class="mt-2 flex flex-col gap-1 text-xs text-muted-foreground" data-testid="lobby-readiness-reasons">
                  {#each launchReadinessMessages as message}
                    <li>{message}</li>
                  {/each}
                </ul>
              {/if}
            </div>
          {/if}

          {#if error}
            <p class="mt-2 text-xs text-red-500" data-testid="lobby-ready-error">{error}</p>
          {/if}
        </Card>
      {/if}

      <Card>
        <h2 class="mb-2 text-sm font-semibold">Invite friends</h2>
        <p class="mb-2 text-xs text-muted-foreground">Share this link with your friends to join.</p>
        <div class="flex items-center gap-2">
          <code
            class="flex-1 truncate rounded border border-border bg-background px-2 py-1 text-xs"
            data-testid="lobby-invite-link">{inviteUrl}</code
          >
          <Button testid="lobby-invite-copy" variant="outline" onclick={() => void copyInvite()}>
            {copied ? 'Copied' : 'Copy'}
          </Button>
          <Button testid="lobby-invite-share" variant="outline" onclick={() => void shareInvite()}>
            Share
          </Button>
        </div>
      </Card>
    </div>

    <Card>
      <h2 class="mb-2 text-sm font-semibold">Roster</h2>
      <p class="mb-3 text-xs text-muted-foreground" data-testid="lobby-composition">
        {#if roster}
          {roster.composition.human_count} humans · {roster.composition.ai_count} AI ·
          {roster.composition.total} seats
        {:else}
          —
        {/if}
      </p>
      {#if roster && roster.members.length > 0}
        <ul class="flex flex-col gap-1" data-testid="lobby-roster">
          {#each roster.members as m (m.member_id)}
            <li
              class="flex flex-wrap items-center justify-between gap-2 text-xs"
              data-testid="lobby-roster-row"
              data-ready={String(m.ready)}
            >
              <span class="flex items-center gap-2">
                <span class={m.present ? 'text-emerald-500' : 'text-muted-foreground'}>●</span>
                <span>{m.display_label}</span>
                {#if m.is_self}<span class="text-muted-foreground">(you)</span>{/if}
                {#if m.host}<span class="text-muted-foreground">(host)</span>{/if}
              </span>
              <span class={m.ready ? 'text-emerald-600' : 'text-muted-foreground'}>
                {m.ready ? 'ready' : 'waiting'}
              </span>
              {#if isHost && !m.is_self}
                <span class="flex items-center gap-1">
                  <Button
                    testid={`lobby-transfer-${m.member_id}`}
                    variant="ghost"
                    disabled={memberActionBusy !== null}
                    onclick={() => void transferHost(m)}
                  >
                    Make host
                  </Button>
                  <Button
                    testid={`lobby-kick-${m.member_id}`}
                    variant="ghost"
                    disabled={memberActionBusy !== null}
                    onclick={() => void kickMember(m)}
                  >
                    Remove
                  </Button>
                </span>
              {/if}
            </li>
          {/each}
        </ul>
      {:else}
        <p class="text-xs text-muted-foreground" data-testid="lobby-roster-empty">No members yet.</p>
      {/if}
      <div class="mt-4 border-t border-border pt-3">
        <h3 class="text-sm font-medium">AI roster</h3>
        <p class="mt-1 text-xs text-muted-foreground">
          Pre-game selections are reshuffled at game start.
        </p>
        {#if summary.ai_roster && summary.ai_roster.length > 0}
          <ul class="mt-3 flex flex-col gap-2" data-testid="lobby-ai-roster">
            {#each summary.ai_roster as model (model.canonical_slug)}
              <li class="rounded border border-violet/30 bg-violet/5 px-3 py-2 text-xs" data-testid="lobby-ai-model-card">
                <span class="font-medium">{model.canonical_slug}</span>
                <span class="ml-2 text-muted-foreground">× {model.count}</span>
              </li>
            {/each}
          </ul>
        {:else}
          <p class="mt-2 text-xs text-muted-foreground" data-testid="lobby-ai-roster-empty">
            The host has not frozen AI model selections yet.
          </p>
        {/if}
      </div>
      <div class="mt-4 border-t border-border pt-3">
        <p class="text-xs text-muted-foreground" data-testid="lobby-stream-state">
          Room updates: {streamState === 'live' ? 'live' : streamState === 'connecting' ? 'connecting' : 'reconnecting'}
        </p>
        <Button
          class="mt-2"
          testid="lobby-leave"
          variant="outline"
          disabled={memberActionBusy !== null}
          onclick={() => void leaveLobby()}
        >
          Leave room
        </Button>
      </div>
    </Card>
  </div>
{/if}
