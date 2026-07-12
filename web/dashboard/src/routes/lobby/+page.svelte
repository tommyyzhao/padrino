<script lang="ts">
  // US-367: Custom tables are configured as pre-game invitations and model
  // slots. They intentionally never map a person or a model to a secret game
  // seat; the server performs the seeded, hidden seating at launch.
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { onMount } from 'svelte';
  import Button from '$lib/components/Button.svelte';
  import Card from '$lib/components/Card.svelte';
  import ModelBadge from '$lib/components/ModelBadge.svelte';
  import { padrino } from '$lib/clientStore.svelte';
  import type {
    FundingPreflightResponse,
    LobbyConfigurationInput,
    OpenRouterConnectionStatus,
    PlayerModelCatalogEntry,
    PromotionalWallet,
    PublicRulesetEntry
  } from '$lib/api/types';

  type FundingSource = 'platform' | 'byok';
  type DeckMode = 'preset' | 'custom';

  interface RoleCard {
    key: string;
    label: string;
    faction: 'Mafia' | 'Town' | 'Solo';
    description: string;
    maxCount: number | null;
  }

  const ROLE_CARDS: RoleCard[] = [
    { key: 'MAFIA_GOON', label: 'Mafia Goon', faction: 'Mafia', description: 'A core Mafia threat.', maxCount: null },
    { key: 'NINJA', label: 'Ninja', faction: 'Mafia', description: 'A stealthy Mafia threat.', maxCount: 1 },
    { key: 'GODFATHER', label: 'Godfather', faction: 'Mafia', description: 'A resilient Mafia leader.', maxCount: 1 },
    { key: 'MAFIA_ROLEBLOCKER', label: 'Mafia Roleblocker', faction: 'Mafia', description: 'Blocks one night action.', maxCount: 1 },
    { key: 'JANITOR', label: 'Janitor', faction: 'Mafia', description: 'Cleans a night result.', maxCount: 1 },
    { key: 'DETECTIVE', label: 'Detective', faction: 'Town', description: 'Investigates at night.', maxCount: 1 },
    { key: 'DOCTOR', label: 'Doctor', faction: 'Town', description: 'Protects at night.', maxCount: 1 },
    { key: 'TRACKER', label: 'Tracker', faction: 'Town', description: 'Tracks one visit.', maxCount: 1 },
    { key: 'WATCHER', label: 'Watcher', faction: 'Town', description: 'Watches one target.', maxCount: 1 },
    { key: 'VILLAGER', label: 'Villager', faction: 'Town', description: 'No night action; joins day discussion and votes.', maxCount: null },
    { key: 'SERIAL_KILLER', label: 'Serial Killer', faction: 'Solo', description: 'A solo night threat.', maxCount: 1 },
    { key: 'JESTER', label: 'Jester', faction: 'Solo', description: 'Wins by being voted out.', maxCount: 1 }
  ];

  const CADENCE_OPTIONS = [
    { id: 'casual_standard_v1', label: 'Casual standard', description: 'Paced 20–30 minute table' },
    { id: 'builtin_sync_v1', label: 'Classic synchronous', description: 'Compact turn cadence' }
  ];

  let rulesets = $state<PublicRulesetEntry[]>([]);
  let rulesetsLoading = $state(true);
  let models = $state<PlayerModelCatalogEntry[]>([]);
  let modelsLoading = $state(true);
  let catalogUnavailable = $state(false);
  let wallet = $state<PromotionalWallet | null>(null);
  let connection = $state<OpenRouterConnectionStatus | null>(null);

  let deckMode = $state<DeckMode>('preset');
  let builtInRulesetId = $state('mini7_v1');
  let customRoleCounts = $state<Record<string, number>>({});
  let cadencePreset = $state('casual_standard_v1');
  let humanSeatCount = $state(1);
  let identityMode = $state<'ANONYMOUS' | 'TRANSPARENT'>('ANONYMOUS');
  let themePackId = $state('noir-laboratory');
  let fundingSource = $state<FundingSource>('platform');
  let byokMaximum = $state('5.00');
  let fillRemaining = $state(true);
  let selectedAiSlot = $state(1);
  let selectedModelsBySlot = $state<Record<number, string>>({});

  let draftLobbyId = $state<string | null>(null);
  let quote = $state<FundingPreflightResponse | null>(null);
  let quoteStale = $state(true);
  let savingQuote = $state(false);
  let inlineIssue = $state<string | null>(null);
  let inviteToken = $state($page.url.searchParams.get('invite') ?? '');
  let joiningInvite = $state(false);
  let inviteIssue = $state<string | null>(null);

  const chosenPreset = $derived(
    rulesets.find((ruleset) => ruleset.ruleset_id === builtInRulesetId) ?? null
  );
  const availableModels = $derived(
    models.filter(
      (model) => model.casual_visible && model.availability === 'available' && !catalogUnavailable
    )
  );
  const selectedAiModels = $derived(
    Object.entries(selectedModelsBySlot)
      .filter((entry): entry is [string, string] => entry[1].length > 0)
      .map(([seatIndex, modelSlug]) => ({
        seat_index: Number(seatIndex),
        model_slug: modelSlug,
        funding_source: fundingSource
      }))
      .sort((a, b) => a.seat_index - b.seat_index)
  );
  const deckValidation = $derived.by(() => validateDeck(customRoleCounts));
  const playerCount = $derived(
    deckMode === 'preset' ? (chosenPreset?.player_count ?? 7) : deckValidation.total
  );
  const aiSlotIndexes = $derived(
    Array.from(
      { length: Math.max(0, playerCount - Math.min(humanSeatCount, playerCount)) },
      (_, offset) => humanSeatCount + offset
    )
  );
  const quoteCanProceed = $derived(
    quote !== null && quote.allowed && quote.funding_quote_hash !== null && !quoteStale
  );

  $effect(() => {
    const capacity = Math.max(1, playerCount);
    if (humanSeatCount > capacity) humanSeatCount = capacity;
    const allowedSlots = new Set(aiSlotIndexes);
    const filteredModelsBySlot = Object.fromEntries(
      Object.entries(selectedModelsBySlot).filter(([slot]) => allowedSlots.has(Number(slot)))
    );
    if (!sameSlotAssignments(selectedModelsBySlot, filteredModelsBySlot)) {
      selectedModelsBySlot = filteredModelsBySlot;
    }
    if (!allowedSlots.has(selectedAiSlot)) selectedAiSlot = aiSlotIndexes[0] ?? 0;
  });

  function sameSlotAssignments(
    left: Record<number, string>,
    right: Record<string, string>
  ): boolean {
    const leftEntries = Object.entries(left);
    const rightEntries = Object.entries(right);
    return (
      leftEntries.length === rightEntries.length &&
      leftEntries.every(([slot, model]) => right[slot] === model)
    );
  }

  function validateDeck(counts: Record<string, number>): {
    ok: boolean;
    total: number;
    explanation: string;
  } {
    const total = Object.values(counts).reduce((sum, count) => sum + count, 0);
    const town = [
      'DETECTIVE',
      'DOCTOR',
      'TRACKER',
      'WATCHER',
      'VILLAGER'
    ].reduce((sum, role) => sum + (counts[role] ?? 0), 0);
    const mafia = ['MAFIA_GOON', 'NINJA', 'GODFATHER', 'MAFIA_ROLEBLOCKER', 'JANITOR'].reduce(
      (sum, role) => sum + (counts[role] ?? 0),
      0
    );
    const solo = (counts.SERIAL_KILLER ?? 0) + (counts.JESTER ?? 0);
    const threat = mafia + (counts.SERIAL_KILLER ?? 0);
    const overCap = ROLE_CARDS.some(
      (card) => card.maxCount !== null && (counts[card.key] ?? 0) > card.maxCount
    );

    if (total === 0) return { ok: false, total, explanation: 'Add roles from supported cards to begin your deck.' };
    if (total < 7 || total > 15) {
      return { ok: false, total, explanation: 'Custom tables need 7–15 total roles.' };
    }
    if (overCap) return { ok: false, total, explanation: 'An advanced role can appear only once.' };
    if (town === 0 || threat === 0) {
      return { ok: false, total, explanation: 'A playable table needs both Town and a night threat.' };
    }
    if (threat >= town) {
      return { ok: false, total, explanation: 'Town must outnumber the active threats at launch.' };
    }
    if (solo > 1) {
      return { ok: false, total, explanation: 'Choose at most one solo role for this supported deck.' };
    }
    return {
      ok: true,
      total,
      explanation: 'Deck ready. The server will freeze this deck and assign roles from its seeded shuffle.'
    };
  }

  function markQuoteStale(): void {
    if (quote !== null) quoteStale = true;
    inlineIssue = null;
  }

  function setDeckMode(mode: DeckMode): void {
    deckMode = mode;
    markQuoteStale();
  }

  function changeRole(role: RoleCard, delta: number): void {
    const next = Math.max(0, (customRoleCounts[role.key] ?? 0) + delta);
    if (role.maxCount !== null && next > role.maxCount) return;
    const copy = { ...customRoleCounts };
    if (next === 0) delete copy[role.key];
    else copy[role.key] = next;
    customRoleCounts = copy;
    markQuoteStale();
  }

  function selectModel(model: PlayerModelCatalogEntry): void {
    const slot = selectedAiSlot || aiSlotIndexes[0];
    if (slot === undefined) {
      inlineIssue = 'Add an AI invitation slot before choosing a model.';
      return;
    }
    selectedModelsBySlot = { ...selectedModelsBySlot, [slot]: model.canonical_slug };
    selectedAiSlot = aiSlotIndexes.find((candidate) => !selectedModelsBySlot[candidate]) ?? slot;
    markQuoteStale();
  }

  function updateSlotModel(slot: number, modelSlug: string): void {
    const copy = { ...selectedModelsBySlot };
    if (modelSlug === '') delete copy[slot];
    else copy[slot] = modelSlug;
    selectedModelsBySlot = copy;
    selectedAiSlot = slot;
    markQuoteStale();
  }

  function selectedRoleCount(role: string): number {
    return customRoleCounts[role] ?? 0;
  }

  function formatMicros(micros: number | null): string {
    if (micros === null) return 'Quote required';
    const dollars = Math.floor(micros / 1_000_000);
    const cents = Math.floor((micros % 1_000_000) / 10_000)
      .toString()
      .padStart(2, '0');
    return `$${dollars}.${cents}`;
  }

  function dollarsToMicros(value: string): number | null {
    const match = /^(0|[1-9]\d*)(?:\.(\d{1,2}))?$/.exec(value.trim());
    if (match === null) return null;
    const whole = Number(match[1]);
    const cents = Number((match[2] ?? '').padEnd(2, '0'));
    if (!Number.isSafeInteger(whole) || !Number.isSafeInteger(cents)) return null;
    return whole * 1_000_000 + cents * 10_000;
  }

  function buildConfiguration(): LobbyConfigurationInput | null {
    if (deckMode === 'custom' && !deckValidation.ok) {
      inlineIssue = deckValidation.explanation;
      return null;
    }
    if (humanSeatCount < 1 || humanSeatCount > playerCount) {
      inlineIssue = 'Choose a human invitation capacity within this table size.';
      return null;
    }
    const source = fundingSource;
    return {
      ...(deckMode === 'preset'
        ? { built_in_ruleset_id: builtInRulesetId }
        : { custom_role_counts: customRoleCounts }),
      cadence_preset: cadencePreset,
      human_seat_count: humanSeatCount,
      identity_mode: identityMode,
      theme_pack_id: themePackId.trim() || null,
      ai_model_selections: selectedAiModels,
      autofill_policy: fillRemaining
        ? { funding_source: source }
        : null
    };
  }

  function describeQuoteFailure(reason: string): string {
    const known: Record<string, string> = {
      byok_required: 'This table needs connected OpenRouter funding for the selected models.',
      byok_maximum_confirmation_required: 'Enter a maximum BYOK spend before requesting this quote.',
      insufficient_promotional_credit: 'Your promotional credit does not cover this table quote.',
      model_unavailable_before_lock: 'One selected model is unavailable. Choose another available model.',
      unpriceable_model: 'One selected model has no platform price. Use connected OpenRouter funding.',
      stale_funding_quote: 'This quote is stale. Refresh it before opening the waiting room.'
    };
    return known[reason] ?? 'This table cannot be funded yet. Review the highlighted configuration and try again.';
  }

  async function ensureGuest(): Promise<void> {
    padrino.setHumanSession(true);
    await padrino.client.createGuest();
  }

  async function requestQuote(): Promise<void> {
    const configuration = buildConfiguration();
    if (configuration === null) return;
    if (fundingSource === 'byok' && connection?.connected !== true) {
      inlineIssue = 'Connect your OpenRouter account before using connected funding.';
      return;
    }
    if (fundingSource === 'platform' && wallet !== null && wallet.available_micros <= 0) {
      inlineIssue = 'No promotional credit is available for this table. Connect OpenRouter or return after credit is available.';
      return;
    }
    const byokMax = fundingSource === 'byok' ? dollarsToMicros(byokMaximum) : null;
    if (fundingSource === 'byok' && (byokMax === null || byokMax <= 0)) {
      inlineIssue = 'Enter a valid maximum BYOK spend using dollars and cents.';
      return;
    }

    savingQuote = true;
    inlineIssue = null;
    try {
      await ensureGuest();
      if (draftLobbyId === null) {
        const created = await padrino.client.createLobby(configuration);
        draftLobbyId = created.id;
      } else {
        await padrino.client.updateLobbyConfiguration(draftLobbyId, configuration);
      }
      const nextQuote = await padrino.client.fundingPreflight(
        draftLobbyId,
        fundingSource === 'byok' ? { byok_max_estimate_micros: byokMax } : {}
      );
      quote = nextQuote;
      quoteStale = false;
      if (!nextQuote.allowed) inlineIssue = describeQuoteFailure(nextQuote.reason);
    } catch (error) {
      const reason =
        typeof error === 'object' && error !== null && 'detail' in error && typeof error.detail === 'string'
          ? error.detail
          : '';
      inlineIssue = describeQuoteFailure(reason);
    } finally {
      savingQuote = false;
    }
  }

  async function joinFromInvite(): Promise<void> {
    if (inviteToken.trim() === '') {
      inviteIssue = 'Paste an invite link token to join your friend’s table.';
      return;
    }
    joiningInvite = true;
    inviteIssue = null;
    try {
      await ensureGuest();
      const lobby = await padrino.client.joinLobby(inviteToken.trim());
      await goto(`/lobby/${encodeURIComponent(lobby.id)}`);
    } catch {
      inviteIssue = 'That invite is not available. Ask the host for a fresh link or check that the room still has capacity.';
    } finally {
      joiningInvite = false;
    }
  }

  async function openWaitingRoom(): Promise<void> {
    const boundQuote = quote;
    if (
      draftLobbyId === null ||
      boundQuote === null ||
      boundQuote.funding_quote_hash === null ||
      !quoteCanProceed
    ) {
      return;
    }
    const params = new URLSearchParams({
      funding_quote_hash: boundQuote.funding_quote_hash,
      funding_source: fundingSource
    });
    if (fundingSource === 'byok' && boundQuote.byok_max_estimate_micros !== null) {
      params.set('byok_max_estimate_micros', String(boundQuote.byok_max_estimate_micros));
    }
    await goto(`/lobby/${encodeURIComponent(draftLobbyId)}?${params.toString()}`);
  }

  async function loadBuilderFacts(): Promise<void> {
    try {
      // Wallet and connection projections are session-scoped. Establish the
      // idempotent guest session before reading them so a first visit does not
      // create noisy 401s or expose a transport failure as a funding state.
      await ensureGuest();
      const [rulesetResponse, modelResponse, walletResponse, connectionResponse] = await Promise.all([
        padrino.client.publicRulesets(),
        padrino.client.listHumanModels({ limit: 48 }),
        padrino.client.getWallet(),
        padrino.client.getOpenRouterConnectionStatus()
      ]);
      rulesets = rulesetResponse.items;
      if (!rulesets.some((ruleset) => ruleset.ruleset_id === builtInRulesetId)) {
        builtInRulesetId = rulesets[0]?.ruleset_id ?? 'mini7_v1';
      }
      models = modelResponse.items;
      catalogUnavailable = modelResponse.catalog.state !== 'current';
      wallet = walletResponse;
      connection = connectionResponse;
      const importedSlugs = ($page.url.searchParams.get('model_slugs') ?? '')
        .split(',')
        .map((slug) => slug.trim().toLowerCase())
        .filter((slug) => availableModels.some((model) => model.canonical_slug === slug));
      if (importedSlugs.length > 0) {
        selectedModelsBySlot = Object.fromEntries(importedSlugs.map((slug, index) => [index + 1, slug]));
      }
    } catch {
      catalogUnavailable = true;
      inlineIssue = 'The table builder could not load its catalog or funding facts. Try again in a moment.';
    } finally {
      rulesetsLoading = false;
      modelsLoading = false;
    }
  }

  onMount(() => {
    void loadBuilderFacts();
  });
</script>

<section class="mx-auto max-w-7xl" aria-labelledby="table-builder-title" data-testid="custom-table-builder">
  <div class="mb-6 max-w-3xl">
    <p class="padrino-eyebrow">Custom laboratory</p>
    <h1 class="mt-2 text-3xl font-bold tracking-tight sm:text-4xl" id="table-builder-title" data-testid="table-builder-title">
      Build a casual table
    </h1>
    <p class="mt-3 text-base leading-7 text-muted-foreground">
      Shape the rules, invitations, AI roster, and funding before friends arrive. This is always a casual private table.
    </p>
  </div>

  <Card class="mb-5" data-testid="lobby-join-panel">
    <div class="flex flex-col gap-3 sm:flex-row sm:items-end">
      <label class="flex min-w-0 flex-1 flex-col gap-1 text-sm"><span class="font-medium">Joining a friend instead?</span><input data-testid="lobby-join-token" bind:value={inviteToken} placeholder="Paste an invite token" class="px-3 py-2" /></label>
      <Button testid="lobby-join-submit" variant="outline" disabled={joiningInvite} onclick={() => void joinFromInvite()}>{joiningInvite ? 'Joining…' : 'Join casual table'}</Button>
    </div>
    {#if inviteIssue}<p class="mt-2 text-sm text-warning" data-testid="lobby-join-error">{inviteIssue}</p>{/if}
  </Card>

  <div class="grid gap-5 xl:grid-cols-[minmax(0,1fr)_22rem]">
    <div class="flex min-w-0 flex-col gap-5">
      <Card>
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="padrino-eyebrow">1 · Rules deck</p>
            <h2 class="mt-1 text-lg font-semibold">Preset or supported role cards</h2>
          </div>
          <div class="flex gap-2" aria-label="Rules deck source">
            <Button testid="table-builder-preset-deck" variant={deckMode === 'preset' ? 'default' : 'outline'} onclick={() => setDeckMode('preset')}>Built-in preset</Button>
            <Button testid="table-builder-custom-deck" variant={deckMode === 'custom' ? 'default' : 'outline'} onclick={() => setDeckMode('custom')}>Custom role deck</Button>
          </div>
        </div>

        {#if deckMode === 'preset'}
          <label class="mt-4 flex max-w-xl flex-col gap-1 text-sm">
            <span class="font-medium">Supported preset</span>
            <select data-testid="table-builder-preset" bind:value={builtInRulesetId} onchange={markQuoteStale} disabled={rulesetsLoading} class="px-3 py-2">
              {#if rulesetsLoading}<option value={builtInRulesetId}>Loading presets…</option>{/if}
              {#each rulesets as ruleset (ruleset.ruleset_id)}
                <option value={ruleset.ruleset_id}>{ruleset.label} · {ruleset.player_count} players</option>
              {/each}
            </select>
          </label>
          <p class="mt-3 text-sm text-muted-foreground">This preset freezes a tested role mix. Choose Custom role deck only when you want to edit supported cards.</p>
        {:else}
          <p class="mt-4 text-sm text-muted-foreground" data-testid="table-builder-role-explanation">
            Role cards determine the hidden deck only. This builder never assigns a chosen person a role; the server shuffles the finalized deck after launch.
          </p>
          <div class="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {#each ROLE_CARDS as role (role.key)}
              <article class="rounded-lg border border-border bg-muted/30 p-3" data-testid={`role-card-${role.key}`}>
                <div class="flex items-start justify-between gap-2"><div><h3 class="text-sm font-semibold">{role.label}</h3><p class="mt-1 text-xs text-muted-foreground">{role.faction} · {role.description}</p></div><span class="font-mono text-lg" aria-label={`${role.label} count`}>{selectedRoleCount(role.key)}</span></div>
                <div class="mt-3 flex gap-2"><Button variant="outline" class="min-h-0 px-2 py-1 text-xs" testid={`role-remove-${role.key}`} disabled={selectedRoleCount(role.key) === 0} onclick={() => changeRole(role, -1)}>Remove</Button><Button class="min-h-0 px-2 py-1 text-xs" testid={`role-add-${role.key}`} disabled={role.maxCount !== null && selectedRoleCount(role.key) >= role.maxCount} onclick={() => changeRole(role, 1)}>Add</Button></div>
              </article>
            {/each}
          </div>
          <p class={deckValidation.ok ? 'mt-4 text-sm text-success' : 'mt-4 text-sm text-warning'} data-testid="table-builder-deck-validation">{deckValidation.ok ? 'Deck ready — ' : 'Deck needs attention — '}{deckValidation.explanation}</p>
        {/if}
      </Card>

      <Card>
        <p class="padrino-eyebrow">2 · Table invitations</p>
        <h2 class="mt-1 text-lg font-semibold">Pre-game roster, not secret seating</h2>
        <div class="mt-4 grid gap-4 md:grid-cols-[13rem_1fr]">
          <label class="flex flex-col gap-1 text-sm"><span class="font-medium">Human invitation slots</span><input data-testid="table-builder-human-slots" type="number" min="1" max={Math.max(1, playerCount)} bind:value={humanSeatCount} oninput={markQuoteStale} class="px-3 py-2" /><span class="text-xs text-muted-foreground">Up to {playerCount} friends can occupy this table. The host uses one slot.</span></label>
          <div class="rounded-lg border border-border bg-muted/20 p-3 text-sm"><p class="font-medium">Identity mode</p><div class="mt-2 flex flex-wrap gap-3"><label class="flex items-center gap-2"><input type="radio" value="ANONYMOUS" bind:group={identityMode} onchange={markQuoteStale} />Anonymous</label><label class="flex items-center gap-2"><input type="radio" value="TRANSPARENT" bind:group={identityMode} onchange={markQuoteStale} />Transparent</label></div><p class="mt-3 text-xs text-muted-foreground" data-testid="table-builder-identity-explanation">{identityMode === 'ANONYMOUS' ? 'Friends can know who joined before play, but no one sees how people or models map to in-game public seats until reveal.' : 'Friends can review the pre-game human roster and selected models; secret roles and the seeded in-game seat mapping stay hidden until reveal.'}</p></div>
        </div>

        <div class="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-3" data-testid="table-builder-slots">
          {#each Array.from({ length: humanSeatCount }) as _, index (index)}
            <div class="rounded-lg border border-cyan/30 bg-cyan/5 p-3"><p class="padrino-eyebrow">Human invitation {index + 1}</p><p class="mt-2 text-sm font-medium">{index === 0 ? 'Host seat' : 'Friend can claim this slot'}</p><p class="mt-1 text-xs text-muted-foreground">Not linked to a future public seat or role.</p></div>
          {/each}
          {#each aiSlotIndexes as slot (slot)}
            <label class="rounded-lg border border-violet/35 bg-violet/5 p-3" data-testid={`ai-slot-card-${slot}`}><span class="padrino-eyebrow">AI model slot {slot - humanSeatCount + 1}</span><select data-testid={`ai-slot-${slot}`} class="mt-2 w-full px-2 py-1 text-sm" value={selectedModelsBySlot[slot] ?? ''} onchange={(event) => updateSlotModel(slot, event.currentTarget.value)}><option value="">{fillRemaining ? 'Curated auto-fill' : 'Choose a model'}</option>{#each availableModels as model (model.canonical_slug)}<option value={model.canonical_slug}>{model.display_name} · {model.canonical_slug}</option>{/each}</select><button class="mt-2 text-left text-xs text-cyan underline" type="button" onclick={() => (selectedAiSlot = slot)}>Assign from model cards</button></label>
          {/each}
        </div>
        {#if aiSlotIndexes.length === 0}<p class="mt-3 text-sm text-warning" data-testid="table-builder-full-human-capacity">Every slot is reserved for humans. Reduce human capacity to add an AI model and receive a model quote.</p>{/if}
      </Card>

      <Card>
        <div class="flex flex-wrap items-start justify-between gap-3"><div><p class="padrino-eyebrow">3 · AI model roster</p><h2 class="mt-1 text-lg font-semibold">Select or duplicate a canonical model</h2></div><a class="text-sm font-semibold underline" href="/models">Explore the full catalog</a></div>
        {#if catalogUnavailable}<p class="mt-4 text-sm text-warning" data-testid="table-builder-catalog-unavailable">The model catalog is unavailable or stale. Refresh before choosing models for a new table.</p>{:else if modelsLoading}<p class="mt-4 text-sm text-muted-foreground">Loading safe model cards…</p>{:else if models.length === 0}<p class="mt-4 text-sm text-muted-foreground">No models are available right now. Curated auto-fill can be reviewed once the catalog returns.</p>{:else}<div class="mt-4 grid gap-3 md:grid-cols-2">{#each models as model (model.canonical_slug)}<article class="rounded-lg border border-border bg-muted/20 p-3" data-testid={model.availability === 'available' ? `model-card-${model.canonical_slug.replaceAll('/', '-')}` : `model-unavailable-${model.canonical_slug.replaceAll('/', '-')}`}><div class="flex items-start justify-between gap-3"><div><h3 class="text-sm font-semibold">{model.display_name}</h3><p class="mt-1 break-all text-xs text-muted-foreground">{model.provider} · {model.canonical_slug}</p></div><ModelBadge tier={model.qualification_tier.replaceAll('_', '-')} label={model.qualification_tier.replaceAll('_', ' ')} /></div><p class="mt-3 text-xs text-muted-foreground">{model.availability === 'available' ? `${model.supports_structured_output ? 'Structured output ready' : 'Casual compatibility'} · ${model.privacy.toUpperCase()}` : `Unavailable: ${model.availability_reason}`}</p><Button class="mt-3" variant="outline" testid={`model-assign-${model.canonical_slug.replaceAll('/', '-')}`} disabled={model.availability !== 'available' || aiSlotIndexes.length === 0} onclick={() => selectModel(model)}>Assign to AI slot {selectedAiSlot > 0 ? selectedAiSlot - humanSeatCount + 1 : ''}</Button></article>{/each}</div>{/if}
        <label class="mt-4 flex items-center gap-2 text-sm"><input type="checkbox" bind:checked={fillRemaining} onchange={markQuoteStale} />Fill unassigned AI slots from the server-curated casual pool</label>
      </Card>
    </div>

    <aside class="flex flex-col gap-5 xl:sticky xl:top-5 xl:self-start">
      <Card>
        <p class="padrino-eyebrow">4 · Cadence and funding</p>
        <h2 class="mt-1 text-lg font-semibold">Freeze a launch quote</h2>
        <label class="mt-4 flex flex-col gap-1 text-sm"><span class="font-medium">Cadence</span><select data-testid="table-builder-cadence" bind:value={cadencePreset} onchange={markQuoteStale} class="px-3 py-2">{#each CADENCE_OPTIONS as cadence (cadence.id)}<option value={cadence.id}>{cadence.label} · {cadence.description}</option>{/each}</select></label>
        <label class="mt-4 flex flex-col gap-1 text-sm"><span class="font-medium">Theme (optional)</span><input class="px-3 py-2" bind:value={themePackId} oninput={markQuoteStale} placeholder="noir-laboratory" /></label>
        <fieldset class="mt-4 flex flex-col gap-2 text-sm"><legend class="font-medium">Funding source</legend><label class="flex items-center gap-2"><input type="radio" value="platform" bind:group={fundingSource} onchange={markQuoteStale} />Use promotional credit{#if wallet} ({formatMicros(wallet.available_micros)} available){/if}</label><label class="flex items-center gap-2"><input type="radio" value="byok" bind:group={fundingSource} onchange={markQuoteStale} aria-label="Use connected OpenRouter funding" />Use connected OpenRouter funding</label></fieldset>
        {#if fundingSource === 'platform' && wallet !== null && wallet.available_micros <= 0}<p class="mt-3 text-sm text-warning" data-testid="table-builder-promo-unavailable">No promotional credit is available. Connect OpenRouter to fund this custom table.</p>{/if}
        {#if fundingSource === 'byok'}<label class="mt-4 flex flex-col gap-1 text-sm"><span class="font-medium">Maximum spend (USD)</span><input data-testid="table-builder-byok-maximum" inputmode="decimal" bind:value={byokMaximum} oninput={markQuoteStale} class="px-3 py-2" />{#if connection?.connected !== true}<span class="text-xs text-warning" data-testid="table-builder-byok-disconnected">Connect your OpenRouter account before the server can bind this table's maximum spend.</span>{:else}<span class="text-xs text-muted-foreground">The maximum is confirmed in the quote and sent again at launch.</span>{/if}</label>{/if}
        <Button class="mt-5 w-full" testid="table-builder-get-quote" disabled={savingQuote || rulesetsLoading || (deckMode === 'custom' && !deckValidation.ok)} onclick={() => void requestQuote()}>{savingQuote ? 'Updating quote…' : quoteStale && quote !== null ? 'Refresh table quote' : 'Get live table quote'}</Button>
        {#if quoteStale && quote !== null}<p class="mt-3 text-sm text-warning" data-testid="table-builder-quote-stale">A deck, cadence, roster, or funding change made this quote stale. Refresh it before launch.</p>{/if}
        {#if quote !== null}<div class="mt-4 rounded-lg border border-border bg-muted/35 p-3 text-sm" data-testid="table-builder-quote"><p class="font-semibold">{quote.allowed ? 'Quote bound to this exact table' : 'Quote needs attention'}</p><dl class="mt-2 grid gap-1 text-xs text-muted-foreground"><div class="flex justify-between gap-3"><dt>Estimated total</dt><dd>{formatMicros(quote.expected_total_micros)}</dd></div><div class="flex justify-between gap-3"><dt>{quote.funding_source === 'BYOK_OWNER' ? 'Confirmed maximum' : 'Promotional reservation'}</dt><dd>{quote.funding_source === 'BYOK_OWNER' ? formatMicros(quote.byok_max_estimate_micros) : formatMicros(quote.reservation_micros)}</dd></div><div class="flex justify-between gap-3"><dt>Funding</dt><dd>{quote.funding_source === 'BYOK_OWNER' ? 'Connected OpenRouter' : 'Promotional credit'}</dd></div></dl></div>{/if}
        {#if inlineIssue}<p class="mt-3 text-sm text-warning" role="alert" data-testid="table-builder-issue">{inlineIssue}</p>{/if}
        <Button class="mt-4 w-full" variant="outline" testid="table-builder-open-waiting-room" disabled={!quoteCanProceed} onclick={() => void openWaitingRoom()}>Open waiting room</Button>
        <p class="mt-3 text-xs text-muted-foreground">Opening the room carries the exact quote confirmation forward. A changed configuration must be quoted again; no credentials are shown here.</p>
      </Card>
    </aside>
  </div>
</section>
