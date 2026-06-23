<script lang="ts">
  // US-154/US-234a: Frontend lobby hub — create a private friend lobby OR join
  // from an invite link. Count-only composition; no per-seat human/AI disclosure
  // (anonymity, AGENTS.md rule 7).
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { onMount } from 'svelte';
  import Card from '$lib/components/Card.svelte';
  import Button from '$lib/components/Button.svelte';
  import { padrino } from '$lib/clientStore.svelte';
  import type { PublicRulesetEntry } from '$lib/api/types';

  // ---- create form state
  let rulesetId = $state('mini7_v1');
  let rulesets = $state<PublicRulesetEntry[]>([]);
  let rulesetsLoading = $state(true);
  let rulesetsError = $state<string | null>(null);
  let identityMode = $state<'ANONYMOUS' | 'TRANSPARENT'>('ANONYMOUS');
  let ranked = $state(false);
  let integrityAcknowledged = $state(false);
  let themePackId = $state('');
  // Bot fill mode: 'autofill' (curated auto-fill) vs 'prepick' (host pre-picks).
  let fillMode = $state<'autofill' | 'prepick'>('autofill');
  let prepickRaw = $state('');

  let creating = $state(false);
  let createError = $state<string | null>(null);

  // ---- join form state. An invite link lands here as /lobby?invite=<token>;
  // we prefill the token so a friend can join in one click.
  let inviteToken = $state($page.url.searchParams.get('invite') ?? '');
  let joining = $state(false);
  let joinError = $state<string | null>(null);

  async function loadRulesets(): Promise<void> {
    rulesetsLoading = true;
    rulesetsError = null;
    try {
      const response = await padrino.client.publicRulesets();
      rulesets = response.items;
      if (rulesets.length > 0 && !rulesets.some((r) => r.ruleset_id === rulesetId)) {
        rulesetId = rulesets[0].ruleset_id;
      }
    } catch (e) {
      rulesets = [];
      rulesetsError = (e as Error).message;
    } finally {
      rulesetsLoading = false;
    }
  }

  async function ensureGuest(): Promise<void> {
    // A guest principal + http-only session cookie is required before any
    // lobby mutation. Minting is idempotent for the page's purposes: the
    // backend issues a fresh guest when none exists.
    padrino.setHumanSession(true);
    await padrino.client.createGuest();
  }

  async function createLobby(): Promise<void> {
    creating = true;
    createError = null;
    try {
      await ensureGuest();
      const prepick =
        fillMode === 'prepick'
          ? prepickRaw
              .split(',')
              .map((s) => s.trim())
              .filter((s) => s.length > 0)
          : [];
      const summary = await padrino.client.createLobby({
        ruleset_id: rulesetId,
        identity_mode: identityMode,
        ranked,
        integrity_acknowledged: ranked ? integrityAcknowledged : false,
        theme_pack_id: themePackId.trim() === '' ? null : themePackId.trim(),
        prepick_agent_build_ids: prepick
      });
      await goto(`/lobby/${encodeURIComponent(summary.id)}`);
    } catch (e) {
      createError = (e as Error).message;
    } finally {
      creating = false;
    }
  }

  async function joinByToken(): Promise<void> {
    if (inviteToken.trim() === '') return;
    joining = true;
    joinError = null;
    try {
      await ensureGuest();
      const summary = await padrino.client.joinLobby(inviteToken.trim());
      await goto(`/lobby/${encodeURIComponent(summary.id)}`);
    } catch (e) {
      joinError = (e as Error).message;
    } finally {
      joining = false;
    }
  }

  onMount(() => {
    void loadRulesets();
  });
</script>

<div class="mb-4">
  <a class="text-sm underline" href="/">← Home</a>
</div>

<h1 class="mb-4 text-xl font-semibold" data-testid="lobby-hub-title">Play with friends</h1>

<div class="grid gap-4 md:grid-cols-2">
  <Card>
    <h2 class="mb-3 text-sm font-semibold">Create a lobby</h2>
    <form
      class="flex flex-col gap-3"
      data-testid="lobby-create-form"
      onsubmit={(e) => {
        e.preventDefault();
        void createLobby();
      }}
    >
      <label class="flex flex-col gap-1 text-xs">
        <span class="font-medium">Ruleset / size</span>
        <select
          class="rounded border border-border bg-background px-2 py-1 text-sm"
          data-testid="lobby-create-ruleset"
          bind:value={rulesetId}
          disabled={rulesetsLoading || rulesets.length === 0}
        >
          {#if rulesetsLoading}
            <option value={rulesetId}>Loading rulesets…</option>
          {:else}
            {#each rulesets as ruleset (ruleset.ruleset_id)}
              <option value={ruleset.ruleset_id}>
                {ruleset.label} ({ruleset.player_count} players)
              </option>
            {/each}
          {/if}
        </select>
      </label>

      {#if rulesetsError}
        <p class="text-xs text-red-500" data-testid="lobby-rulesets-error">{rulesetsError}</p>
      {/if}

      <label class="flex flex-col gap-1 text-xs">
        <span class="font-medium">Identity mode</span>
        <select
          class="rounded border border-border bg-background px-2 py-1 text-sm"
          data-testid="lobby-create-identity-mode"
          bind:value={identityMode}
        >
          <option value="ANONYMOUS">Anonymous (guess who is the AI)</option>
          <option value="TRANSPARENT">Transparent</option>
        </select>
      </label>

      <label class="flex items-center gap-2 text-xs">
        <input
          type="checkbox"
          class="h-4 w-4"
          data-testid="lobby-create-ranked"
          bind:checked={ranked}
        />
        <span class="font-medium">Ranked</span>
      </label>

      {#if ranked}
        <label class="flex items-center gap-2 text-xs">
          <input
            type="checkbox"
            class="h-4 w-4"
            data-testid="lobby-create-integrity-ack"
            bind:checked={integrityAcknowledged}
          />
          <span class="font-medium">Ranked integrity acknowledged</span>
        </label>
      {/if}

      <label class="flex flex-col gap-1 text-xs">
        <span class="font-medium">Theme pack</span>
        <input
          class="rounded border border-border bg-background px-2 py-1 text-sm"
          data-testid="lobby-create-theme"
          placeholder="default"
          bind:value={themePackId}
        />
      </label>

      <fieldset class="flex flex-col gap-1 text-xs" data-testid="lobby-create-fill-mode">
        <span class="font-medium">Bots</span>
        <label class="flex items-center gap-2">
          <input type="radio" value="autofill" bind:group={fillMode} data-testid="lobby-fill-autofill" />
          <span>Curated auto-fill</span>
        </label>
        <label class="flex items-center gap-2">
          <input type="radio" value="prepick" bind:group={fillMode} data-testid="lobby-fill-prepick" />
          <span>Host pre-pick</span>
        </label>
      </fieldset>

      {#if fillMode === 'prepick'}
        <label class="flex flex-col gap-1 text-xs">
          <span class="font-medium">Pre-pick agent build ids (comma-separated)</span>
          <input
            class="rounded border border-border bg-background px-2 py-1 text-sm"
            data-testid="lobby-create-prepick"
            bind:value={prepickRaw}
          />
        </label>
      {/if}

      <p class="text-xs text-muted-foreground" data-testid="lobby-create-stakes">
        Mode: <span class="font-semibold">{ranked ? 'RANKED' : 'CASUAL'}</span>
      </p>

      <Button
        type="submit"
        testid="lobby-create-submit"
        disabled={creating || rulesets.length === 0 || (ranked && !integrityAcknowledged)}
      >
        {creating ? 'Creating…' : 'Create lobby'}
      </Button>

      {#if createError}
        <p class="text-xs text-red-500" data-testid="lobby-create-error">{createError}</p>
      {/if}
    </form>
  </Card>

  <Card>
    <h2 class="mb-3 text-sm font-semibold">Join from an invite</h2>
    <form
      class="flex flex-col gap-3"
      data-testid="lobby-join-form"
      onsubmit={(e) => {
        e.preventDefault();
        void joinByToken();
      }}
    >
      <label class="flex flex-col gap-1 text-xs">
        <span class="font-medium">Invite token</span>
        <input
          class="rounded border border-border bg-background px-2 py-1 text-sm"
          data-testid="lobby-join-token"
          placeholder="paste your invite token"
          bind:value={inviteToken}
        />
      </label>
      <Button type="submit" testid="lobby-join-submit" variant="outline" disabled={joining}>
        {joining ? 'Joining…' : 'Join as guest'}
      </Button>
      <p class="text-xs text-muted-foreground">No signup needed — you join instantly as a guest.</p>
      {#if joinError}
        <p class="text-xs text-red-500" data-testid="lobby-join-error">{joinError}</p>
      {/if}
    </form>
  </Card>
</div>
