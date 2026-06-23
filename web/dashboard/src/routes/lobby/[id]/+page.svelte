<script lang="ts">
  // US-154: Lobby detail — invite share, consent + 16+ gate, count-only roster,
  // ready / host controls, start (launch). Identity-blind: the roster never
  // exposes a per-seat human/AI map, only counts (AGENTS.md rule 7).
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import { onMount, onDestroy } from 'svelte';
  import Card from '$lib/components/Card.svelte';
  import Button from '$lib/components/Button.svelte';
  import { padrino } from '$lib/clientStore.svelte';
  import type { LobbyRoster, LobbySummary } from '$lib/api/types';

  let lobbyId = $derived($page.params.id);

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

  let pollTimer: ReturnType<typeof setInterval> | null = null;

  // The host can launch; non-hosts only ready up. We cannot reveal which member
  // is which seat, but the lobby summary names the host principal and the guest
  // summary names our own principal — comparing them is identity-mode-neutral
  // (it discloses NOTHING about human/AI seat assignment).
  let myPrincipalId = $state<string | null>(null);
  let isHost = $derived(
    summary !== null && myPrincipalId !== null && summary.host_principal_id === myPrincipalId
  );

  const inviteUrl = $derived(
    summary === null
      ? ''
      : typeof window === 'undefined'
        ? summary.invite_token
        : `${window.location.origin}/lobby?invite=${encodeURIComponent(summary.invite_token)}`
  );

  // All members ready is the gate the host needs before launch makes sense.
  let allReady = $derived(
    roster !== null && roster.members.length > 0 && roster.members.every((m) => m.ready)
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
      myPrincipalId = me.principal_id;
      if (me.display_name) displayName = me.display_name;
      error = null;
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  }

  async function refreshRoster(): Promise<void> {
    if (!lobbyId) return;
    try {
      // Heartbeat keeps our presence alive and returns the current roster.
      roster = await padrino.client.lobbyHeartbeat(lobbyId);
    } catch {
      // transient — keep the last roster
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
      error = (e as Error).message;
    } finally {
      consenting = false;
    }
  }

  async function toggleReady(next: boolean): Promise<void> {
    if (!lobbyId) return;
    readyBusy = true;
    try {
      roster = await padrino.client.setLobbyReady(lobbyId, next);
    } catch (e) {
      error = (e as Error).message;
    } finally {
      readyBusy = false;
    }
  }

  async function startGame(): Promise<void> {
    if (!lobbyId) return;
    launching = true;
    error = null;
    try {
      await padrino.client.lockLobby(lobbyId);
      const launched = await padrino.client.launchLobby(lobbyId);
      await goto(`/play/${encodeURIComponent(launched.game_id)}`);
    } catch (e) {
      error = (e as Error).message;
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

  // My own ready flag: the roster is identity-blind, so we surface ready/start
  // controls without claiming a seat. We track our intended ready locally and
  // reflect the roster's aggregate.
  let iAmReady = $state(false);

  onMount(() => {
    void loadAll();
    pollTimer = setInterval(() => void refreshRoster(), 3000);
  });

  onDestroy(() => {
    if (pollTimer) clearInterval(pollTimer);
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
                iAmReady = !iAmReady;
                void toggleReady(iAmReady);
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
              <Button
                testid="lobby-start"
                disabled={launching || !allReady}
                onclick={() => void startGame()}
              >
                {launching ? 'Starting…' : 'Start game'}
              </Button>
              {#if !allReady}
                <p class="mt-1 text-xs text-muted-foreground" data-testid="lobby-start-hint">
                  Waiting for all members to ready up.
                </p>
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
              class="flex items-center justify-between text-xs"
              data-testid="lobby-roster-row"
              data-ready={String(m.ready)}
            >
              <span class="flex items-center gap-2">
                <span class={m.present ? 'text-emerald-500' : 'text-muted-foreground'}>●</span>
                <span class="font-mono">{m.member_id.slice(0, 8)}</span>
                {#if m.is_host}<span class="text-muted-foreground">(host)</span>{/if}
              </span>
              <span class={m.ready ? 'text-emerald-600' : 'text-muted-foreground'}>
                {m.ready ? 'ready' : 'waiting'}
              </span>
            </li>
          {/each}
        </ul>
      {:else}
        <p class="text-xs text-muted-foreground" data-testid="lobby-roster-empty">No members yet.</p>
      {/if}
    </Card>
  </div>
{/if}
