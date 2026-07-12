<script lang="ts">
  import { onMount } from 'svelte';
  import type { PlayerModelCatalogEntry } from '$lib/api/types';
  import Button from '$lib/components/Button.svelte';
  import Card from '$lib/components/Card.svelte';
  import Dialog from '$lib/components/Dialog.svelte';
  import ModelBadge from '$lib/components/ModelBadge.svelte';
  import { padrino } from '$lib/clientStore.svelte';

  const CATALOG_PAGE_SIZE = 200;
  const CARD_RENDER_BATCH_SIZE = 24;
  const LOW_PRICE_MICROS_PER_MILLION = 1_000_000;
  const EXTREME_PRICE_MICROS_PER_MILLION = 5_000_000;
  const ESTIMATED_GAME_PROMPT_TOKENS = [100_000, 300_000] as const;
  const ESTIMATED_GAME_COMPLETION_TOKENS = [40_000, 120_000] as const;

  type PriceBand = 'all' | 'free' | 'low' | 'mid' | 'high' | 'unpriced';
  type ContextBand = 'all' | '32k' | '128k' | '256k';
  type SpeedBand = 'all' | 'fast' | 'steady' | 'measured';
  type PendingConfirmation = 'reliability' | 'byok' | null;

  let models = $state<PlayerModelCatalogEntry[]>([]);
  let nextCursor = $state<string | null>(null);
  let catalogState = $state<'current' | 'stale' | 'unavailable'>('current');
  let loading = $state(true);
  let loadingMore = $state(false);
  let catalogError = $state<string | null>(null);
  let search = $state('');
  let provider = $state('all');
  let priceBand = $state<PriceBand>('all');
  let structuredOnly = $state(false);
  let contextBand = $state<ContextBand>('all');
  let speedBand = $state<SpeedBand>('all');
  let openWeightOnly = $state(false);
  let frontierOnly = $state(false);
  let platformEligibleOnly = $state(false);
  let byokOnly = $state(false);
  let rankedPreview = $state(false);
  let visibleCardCount = $state(CARD_RENDER_BATCH_SIZE);
  let compareSlugs = $state<string[]>([]);
  let selectedSlugs = $state<string[]>([]);
  let pendingModel = $state<PlayerModelCatalogEntry | null>(null);
  let pendingConfirmation = $state<PendingConfirmation>(null);
  let byokAcknowledged = $state(false);
  let selectionError = $state<string | null>(null);
  let requestVersion = 0;

  const providers = $derived(
    [...new Set(models.map((model) => model.provider))].sort((left, right) => left.localeCompare(right))
  );
  const filteredModels = $derived(models.filter(matchesFilters));
  const renderedModels = $derived(filteredModels.slice(0, visibleCardCount));
  const comparedModels = $derived(
    compareSlugs
      .map((slug) => models.find((model) => model.canonical_slug === slug))
      .filter((model): model is PlayerModelCatalogEntry => model !== undefined)
  );

  function lower(value: string): string {
    return value.toLocaleLowerCase();
  }

  function priceValues(model: PlayerModelCatalogEntry): [number | null, number | null] {
    return [
      model.pricing_per_million_micros.prompt_per_million ?? null,
      model.pricing_per_million_micros.completion_per_million ?? null
    ];
  }

  function maximumPrice(model: PlayerModelCatalogEntry): number | null {
    const [promptPrice, completionPrice] = priceValues(model);
    return promptPrice === null || completionPrice === null
      ? null
      : Math.max(promptPrice, completionPrice);
  }

  function matchesPriceBand(model: PlayerModelCatalogEntry): boolean {
    const [promptPrice, completionPrice] = priceValues(model);
    const maximum = maximumPrice(model);
    if (priceBand === 'all') return true;
    if (priceBand === 'unpriced') return maximum === null;
    if (promptPrice === 0 && completionPrice === 0) return priceBand === 'free';
    if (maximum === null) return false;
    if (priceBand === 'low') return maximum <= LOW_PRICE_MICROS_PER_MILLION;
    if (priceBand === 'mid') {
      return maximum > LOW_PRICE_MICROS_PER_MILLION && maximum <= EXTREME_PRICE_MICROS_PER_MILLION;
    }
    return priceBand === 'high' && maximum > EXTREME_PRICE_MICROS_PER_MILLION;
  }

  function meetsContextBand(model: PlayerModelCatalogEntry): boolean {
    const minimums: Record<ContextBand, number> = {
      all: 0,
      '32k': 32_000,
      '128k': 128_000,
      '256k': 256_000
    };
    return contextBand === 'all' || (model.context_length !== null && model.context_length >= minimums[contextBand]);
  }

  function matchesSpeedBand(model: PlayerModelCatalogEntry): boolean {
    if (speedBand === 'all') return true;
    if (speedBand === 'measured') return model.observed_latency_ms !== null;
    if (model.observed_latency_ms === null) return false;
    return speedBand === 'fast'
      ? model.observed_latency_ms <= 1_000
      : model.observed_latency_ms <= 2_500;
  }

  function matchesFilters(model: PlayerModelCatalogEntry): boolean {
    const searchable = `${model.display_name} ${model.canonical_slug} ${model.provider}`;
    if (search.trim() && !lower(searchable).includes(lower(search.trim()))) return false;
    if (provider !== 'all' && model.provider !== provider) return false;
    if (!matchesPriceBand(model)) return false;
    if (structuredOnly && !model.supports_structured_output) return false;
    if (!meetsContextBand(model)) return false;
    if (!matchesSpeedBand(model)) return false;
    if (openWeightOnly && model.open_weights !== true) return false;
    if (frontierOnly && model.frontier !== true) return false;
    if (platformEligibleOnly && !model.platform_eligible) return false;
    if (byokOnly && !model.requires_byok) return false;
    return !rankedPreview || model.qualification_tier === 'ranked_qualified';
  }

  function currentQuery(cursor?: string): Parameters<typeof padrino.client.listHumanModels>[0] {
    const contextMinimums: Record<ContextBand, number | undefined> = {
      all: undefined,
      '32k': 32_000,
      '128k': 128_000,
      '256k': 256_000
    };
    const maximumPrice =
      priceBand === 'low'
        ? LOW_PRICE_MICROS_PER_MILLION
        : priceBand === 'mid'
          ? EXTREME_PRICE_MICROS_PER_MILLION
          : undefined;
    return {
      search: search.trim() || undefined,
      provider: provider === 'all' ? undefined : provider,
      max_price_micros_per_million: maximumPrice,
      min_context_length: contextMinimums[contextBand],
      requires_structured_output: structuredOnly || undefined,
      tier: rankedPreview ? 'ranked_qualified' : undefined,
      open_weight: openWeightOnly || undefined,
      frontier: frontierOnly || undefined,
      platform_eligible: platformEligibleOnly || undefined,
      byok_only: byokOnly || undefined,
      limit: CATALOG_PAGE_SIZE,
      cursor
    };
  }

  async function refreshCatalog(): Promise<void> {
    const version = ++requestVersion;
    loading = true;
    catalogError = null;
    visibleCardCount = CARD_RENDER_BATCH_SIZE;
    try {
      const page = await padrino.client.listHumanModels(currentQuery());
      if (version !== requestVersion) return;
      models = page.items;
      nextCursor = page.next_cursor;
      catalogState = page.catalog.state;
    } catch {
      if (version !== requestVersion) return;
      models = [];
      nextCursor = null;
      catalogState = 'unavailable';
      catalogError = 'The model catalog could not be reached. Try again in a moment.';
    } finally {
      if (version === requestVersion) loading = false;
    }
  }

  async function loadMoreCatalog(): Promise<void> {
    if (nextCursor === null || loadingMore) return;
    loadingMore = true;
    try {
      const page = await padrino.client.listHumanModels(currentQuery(nextCursor));
      const known = new Set(models.map((model) => model.canonical_slug));
      models = [...models, ...page.items.filter((model) => !known.has(model.canonical_slug))];
      nextCursor = page.next_cursor;
      catalogState = page.catalog.state;
    } catch {
      catalogError = 'More models could not be loaded. Your current results are still available.';
    } finally {
      loadingMore = false;
    }
  }

  function refreshForFilter(): void {
    void refreshCatalog();
  }

  function formatMicros(micros: number): string {
    const whole = Math.floor(micros / 1_000_000);
    const cents = Math.floor((micros % 1_000_000) / 10_000)
      .toString()
      .padStart(2, '0');
    return `$${whole}.${cents}`;
  }

  function formatPrice(micros: number | null): string {
    return micros === null ? 'Unpriced' : `${formatMicros(micros)} / 1M`;
  }

  function formatContext(contextLength: number | null): string {
    return contextLength === null ? 'Context not listed' : `${Math.round(contextLength / 1_000)}k context`;
  }

  function estimatedGameRange(model: PlayerModelCatalogEntry): string {
    const [promptPrice, completionPrice] = priceValues(model);
    if (promptPrice === null || completionPrice === null) return 'Table quote required';
    const low =
      (promptPrice * ESTIMATED_GAME_PROMPT_TOKENS[0] + completionPrice * ESTIMATED_GAME_COMPLETION_TOKENS[0]) /
      1_000_000;
    const high =
      (promptPrice * ESTIMATED_GAME_PROMPT_TOKENS[1] + completionPrice * ESTIMATED_GAME_COMPLETION_TOKENS[1]) /
      1_000_000;
    return `${formatMicros(Math.ceil(low))}–${formatMicros(Math.ceil(high))}`;
  }

  function tierLabel(model: PlayerModelCatalogEntry): string {
    return model.qualification_tier.replaceAll('_', ' ');
  }

  function selectable(model: PlayerModelCatalogEntry): boolean {
    return model.casual_visible && model.availability === 'available' && catalogState === 'current';
  }

  function toggleComparison(slug: string): void {
    if (compareSlugs.includes(slug)) {
      compareSlugs = compareSlugs.filter((candidate) => candidate !== slug);
      return;
    }
    if (compareSlugs.length < 3) compareSlugs = [...compareSlugs, slug];
  }

  function addSelection(model: PlayerModelCatalogEntry): void {
    selectedSlugs = selectedSlugs.includes(model.canonical_slug)
      ? selectedSlugs
      : [...selectedSlugs, model.canonical_slug];
    pendingModel = null;
    pendingConfirmation = null;
    byokAcknowledged = false;
  }

  function requestSelection(model: PlayerModelCatalogEntry): void {
    selectionError = null;
    if (!selectable(model)) {
      selectionError = 'This model is not available for a fresh casual table yet. Refresh the catalog and try again.';
      return;
    }
    pendingModel = model;
    pendingConfirmation = model.qualification_tier === 'unverified' ? 'reliability' : model.requires_byok ? 'byok' : null;
    if (pendingConfirmation === null) addSelection(model);
  }

  function continueReliability(): void {
    if (pendingModel === null) return;
    pendingConfirmation = pendingModel.requires_byok ? 'byok' : null;
    if (pendingConfirmation === null) addSelection(pendingModel);
  }

  function continueByok(): void {
    if (pendingModel === null || !byokAcknowledged) return;
    addSelection(pendingModel);
  }

  function tableBuilderHref(): string {
    return `/lobby?model_slugs=${encodeURIComponent(selectedSlugs.join(','))}`;
  }

  onMount(() => {
    void refreshCatalog();
  });
</script>

<section class="mx-auto max-w-7xl" aria-labelledby="model-explorer-title">
  <div class="mb-6 max-w-3xl">
    <p class="padrino-eyebrow">OpenRouter catalog</p>
    <h1 class="mt-2 text-3xl font-bold tracking-tight sm:text-4xl" data-testid="model-explorer-title" id="model-explorer-title">
      Model explorer
    </h1>
    <p class="mt-3 text-base leading-7 text-muted-foreground">
      Compare models by the canonical OpenRouter slug that will be frozen into a casual table.
      Prices are per million tokens; a final whole-game quote is always bound before launch.
    </p>
  </div>

  {#if catalogState === 'stale'}
    <Card class="mb-5 border-warning/50" data-testid="model-catalog-stale">
      <p class="font-semibold text-warning">Catalog refresh pending</p>
      <p class="mt-1 text-sm text-muted-foreground">
        These model facts are stale. Review a fresh table quote before you launch.
      </p>
      <Button class="mt-3" variant="outline" testid="model-catalog-retry" onclick={() => void refreshCatalog()}>
        Refresh catalog
      </Button>
    </Card>
  {/if}

  <Card class="mb-5" data-testid="model-filter-panel">
    <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <label class="flex flex-col gap-1 text-sm sm:col-span-2">
        <span class="font-medium">Search models</span>
        <input
          aria-label="Search models"
          class="px-3 py-2"
          placeholder="Name, provider, or canonical slug"
          bind:value={search}
          oninput={refreshForFilter}
        />
      </label>
      <label class="flex flex-col gap-1 text-sm">
        <span class="font-medium">Provider</span>
        <select aria-label="Provider filter" class="px-3 py-2" bind:value={provider} onchange={refreshForFilter}>
          <option value="all">All providers</option>
          {#each providers as providerName}
            <option value={providerName}>{providerName}</option>
          {/each}
        </select>
      </label>
      <label class="flex flex-col gap-1 text-sm">
        <span class="font-medium">Price</span>
        <select aria-label="Price filter" class="px-3 py-2" bind:value={priceBand} onchange={refreshForFilter}>
          <option value="all">Any price</option>
          <option value="free">Free</option>
          <option value="low">Up to $1 / 1M</option>
          <option value="mid">$1–$5 / 1M</option>
          <option value="high">Over $5 / 1M</option>
          <option value="unpriced">Unpriced</option>
        </select>
      </label>
      <label class="flex flex-col gap-1 text-sm">
        <span class="font-medium">Context</span>
        <select aria-label="Context filter" class="px-3 py-2" bind:value={contextBand} onchange={refreshForFilter}>
          <option value="all">Any context</option>
          <option value="32k">32k+</option>
          <option value="128k">128k+</option>
          <option value="256k">256k+</option>
        </select>
      </label>
      <label class="flex flex-col gap-1 text-sm">
        <span class="font-medium">Speed</span>
        <select aria-label="Speed filter" class="px-3 py-2" bind:value={speedBand} onchange={refreshForFilter}>
          <option value="all">Any speed</option>
          <option value="fast">Fast (≤ 1s)</option>
          <option value="steady">Steady (≤ 2.5s)</option>
          <option value="measured">Measured only</option>
        </select>
      </label>
    </div>
    <fieldset class="mt-4 flex flex-wrap gap-x-5 gap-y-3 text-sm">
      <legend class="sr-only">Catalog capability filters</legend>
      <label class="flex items-center gap-2"><input type="checkbox" bind:checked={structuredOnly} onchange={refreshForFilter} aria-label="Structured output only" />Structured output</label>
      <label class="flex items-center gap-2"><input type="checkbox" bind:checked={openWeightOnly} onchange={refreshForFilter} aria-label="Open-weight filter" />Open-weight</label>
      <label class="flex items-center gap-2"><input type="checkbox" bind:checked={frontierOnly} onchange={refreshForFilter} aria-label="Frontier filter" />Frontier</label>
      <label class="flex items-center gap-2"><input type="checkbox" bind:checked={platformEligibleOnly} onchange={refreshForFilter} aria-label="Platform-eligible filter" />Platform eligible</label>
      <label class="flex items-center gap-2"><input type="checkbox" bind:checked={byokOnly} onchange={refreshForFilter} aria-label="BYOK-only filter" />BYOK only</label>
      <label class="flex items-center gap-2"><input type="checkbox" bind:checked={rankedPreview} onchange={refreshForFilter} aria-label="Ranked-qualified preview" />Ranked-qualified preview</label>
    </fieldset>
  </Card>

  {#if catalogError}
    <Card class="mb-5 border-danger/50" data-testid="model-catalog-error">
      <p class="font-semibold text-danger">{catalogError}</p>
      <Button class="mt-3" variant="outline" testid="model-catalog-retry" onclick={() => void refreshCatalog()}>Retry catalog</Button>
    </Card>
  {/if}

  {#if compareSlugs.length > 0}
    <Card class="mb-5 overflow-x-auto" data-testid="model-comparison">
      <div class="flex items-start justify-between gap-4">
        <div>
          <p class="padrino-eyebrow">Comparison tray</p>
          <h2 class="mt-1 text-lg font-semibold">Compare up to three canonical selections</h2>
        </div>
        <Button variant="ghost" onclick={() => (compareSlugs = [])}>Clear comparison</Button>
      </div>
      <table class="mt-4 min-w-full text-left text-sm">
        <thead><tr class="border-b border-border"><th class="p-2">Model</th><th class="p-2">Price / 1M</th><th class="p-2">Context</th><th class="p-2">Speed</th><th class="p-2">Privacy</th></tr></thead>
        <tbody>
          {#each comparedModels as model (model.canonical_slug)}
            <tr class="border-b border-border/70"><td class="p-2 font-medium">{model.canonical_slug}</td><td class="p-2">{formatPrice(priceValues(model)[0])} / {formatPrice(priceValues(model)[1])}</td><td class="p-2">{formatContext(model.context_length)}</td><td class="p-2">{model.observed_latency_ms === null ? 'Not measured' : `${model.observed_latency_ms} ms`}</td><td class="p-2">{model.privacy.toUpperCase()}</td></tr>
          {/each}
        </tbody>
      </table>
    </Card>
  {/if}

  {#if selectedSlugs.length > 0}
    <Card class="mb-5" data-testid="model-selection">
      <p class="padrino-eyebrow">Table selections</p>
      <h2 class="mt-1 text-lg font-semibold">Canonical model slugs selected</h2>
      <ul class="mt-3 flex flex-wrap gap-2" aria-label="Selected model slugs">
        {#each selectedSlugs as slug (slug)}<li><code class="rounded bg-muted px-2 py-1 text-xs">{slug}</code></li>{/each}
      </ul>
      <a class="mt-4 inline-flex text-sm font-semibold underline" href={tableBuilderHref()}>Use these in a custom table</a>
    </Card>
  {/if}

  {#if selectionError}<p class="mb-4 text-sm text-danger" role="alert">{selectionError}</p>{/if}

  {#if loading}
    <p class="py-10 text-sm text-muted-foreground" aria-live="polite">Loading the model catalog…</p>
  {:else if filteredModels.length === 0}
    <Card data-testid="model-empty-state"><h2 class="text-lg font-semibold">No models match these filters</h2><p class="mt-2 text-sm text-muted-foreground">Broaden a filter or wait for the catalog to refresh.</p></Card>
  {:else}
    <p class="mb-3 text-sm text-muted-foreground" aria-live="polite">Showing {renderedModels.length} of {filteredModels.length} loaded models. Cards render in small batches for responsive browsing.</p>
    <div class="grid gap-4 md:grid-cols-2 xl:grid-cols-3" data-testid="model-virtual-list">
      {#each renderedModels as model (model.canonical_slug)}
        <Card class="flex flex-col" data-testid="model-card">
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0"><h2 class="break-words text-lg font-semibold">{model.display_name}</h2><p class="mt-1 break-all text-xs text-muted-foreground">{model.provider} · {model.canonical_slug}</p></div>
            <ModelBadge tier={model.qualification_tier.replaceAll('_', '-')} label={tierLabel(model)} />
          </div>
          {#if model.description}<p class="mt-3 text-sm leading-6 text-muted-foreground">{model.description}</p>{/if}
          <dl class="mt-4 grid grid-cols-2 gap-x-3 gap-y-3 text-xs">
            <div><dt class="font-medium text-muted-foreground">Context</dt><dd>{formatContext(model.context_length)}</dd></div>
            <div><dt class="font-medium text-muted-foreground">Privacy</dt><dd>{model.privacy === 'zdr' ? 'ZDR' : model.privacy}</dd></div>
            <div><dt class="font-medium text-muted-foreground">Prompt price</dt><dd>{formatPrice(priceValues(model)[0])}</dd></div>
            <div><dt class="font-medium text-muted-foreground">Completion price</dt><dd>{formatPrice(priceValues(model)[1])}</dd></div>
            <div><dt class="font-medium text-muted-foreground">Estimated game range</dt><dd>{estimatedGameRange(model)}</dd></div>
            <div><dt class="font-medium text-muted-foreground">Availability</dt><dd>{model.availability}</dd></div>
            {#if model.observed_latency_ms !== null}<div><dt class="font-medium text-muted-foreground">Latency</dt><dd>{model.observed_latency_ms} ms</dd></div>{/if}
            {#if model.observed_throughput_tokens_per_second !== null}<div><dt class="font-medium text-muted-foreground">Throughput</dt><dd>{model.observed_throughput_tokens_per_second} tok/s</dd></div>{/if}
          </dl>
          {#if model.qualification_reasons.length > 0}<p class="mt-3 text-xs text-muted-foreground">{model.qualification_reasons.join(' · ')}</p>{/if}
          <div class="mt-4 flex flex-wrap gap-2">
            {#if model.open_weights === true}<span class="padrino-badge model-playable">Open-weight</span>{/if}
            {#if model.frontier === true}<span class="padrino-badge model-ranked">Frontier</span>{/if}
            {#if model.platform_eligible}<span class="padrino-badge status-ready">Platform eligible</span>{/if}
            {#if model.requires_byok}<span class="padrino-badge status-warning">BYOK required</span>{/if}
          </div>
          <div class="mt-5 flex flex-wrap gap-2">
            <Button variant="outline" testid={`model-compare-${model.canonical_slug}`} onclick={() => toggleComparison(model.canonical_slug)} disabled={!compareSlugs.includes(model.canonical_slug) && compareSlugs.length >= 3}>{compareSlugs.includes(model.canonical_slug) ? 'Remove comparison' : 'Compare'}</Button>
            <Button testid={`model-select-${model.canonical_slug}`} onclick={() => requestSelection(model)} disabled={!selectable(model)}>{selectedSlugs.includes(model.canonical_slug) ? 'Selected' : 'Select model'}</Button>
          </div>
        </Card>
      {/each}
    </div>
    {#if visibleCardCount < filteredModels.length}<Button class="mt-5" variant="outline" onclick={() => (visibleCardCount += CARD_RENDER_BATCH_SIZE)}>Show more models</Button>{/if}
    {#if nextCursor !== null}<Button class="mt-5 ml-2" variant="outline" onclick={() => void loadMoreCatalog()} disabled={loadingMore}>{loadingMore ? 'Loading models…' : 'Load next catalog page'}</Button>{/if}
  {/if}
</section>

<Dialog id="model-reliability-confirmation" title="Use an unverified casual model" description="This model has not yet passed structured compatibility checks. It may miss actions or need more supervision in a casual table." open={pendingConfirmation === 'reliability'} onclose={() => (pendingConfirmation = null)}>
  <div data-testid="model-reliability-confirmation"><p class="text-sm">Continue only if you are comfortable with a casual reliability risk. Ranked play will not use this selection.</p><div class="mt-5 flex justify-end gap-3"><Button variant="outline" onclick={() => (pendingConfirmation = null)}>Cancel</Button><Button testid="model-reliability-continue" onclick={continueReliability}>Use casually</Button></div></div>
</Dialog>

<Dialog id="model-byok-confirmation" title="BYOK funding required" description="This model is unpriced or exceeds the platform-funded price guard. A connected OpenRouter account and a maximum-spend confirmation are required before launch." open={pendingConfirmation === 'byok'} onclose={() => (pendingConfirmation = null)}>
  <div data-testid="model-byok-confirmation"><label class="flex gap-2 text-sm"><input type="checkbox" bind:checked={byokAcknowledged} aria-label="I understand this model needs my connected OpenRouter account" />I understand this model needs my connected OpenRouter account and I will confirm its maximum spend in the table builder.</label><div class="mt-5 flex justify-end gap-3"><Button variant="outline" onclick={() => (pendingConfirmation = null)}>Cancel</Button><Button testid="model-byok-continue" disabled={!byokAcknowledged} onclick={continueByok}>Confirm BYOK requirement</Button></div></div>
</Dialog>
