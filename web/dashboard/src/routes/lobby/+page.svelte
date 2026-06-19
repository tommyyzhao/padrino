<script lang="ts">
  // US-154: Frontend lobby hub — create a private friend lobby OR join from an
  // invite link. Casual-only (stakes shown CASUAL); count-only composition; no
  // per-seat human/AI disclosure (anonymity, AGENTS.md rule 7).
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import Card from '$lib/components/Card.svelte';
  import Button from '$lib/components/Button.svelte';
  import { padrino } from '$lib/clientStore.svelte';

  // ---- create form state
  let rulesetId = $state<'mini7_v1' | 'bench10_v1'>('mini7_v1');
  let identityMode = $state<'ANONYMOUS' | 'TRANSPARENT'>('ANONYMOUS');
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
        >
          <option value="mini7_v1">mini7_v1 (7 players)</option>
          <option value="bench10_v1">bench10_v1 (10 players)</option>
        </select>
      </label>

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
        Stakes: <span class="font-semibold">CASUAL</span>
      </p>

      <Button type="submit" testid="lobby-create-submit" disabled={creating}>
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
