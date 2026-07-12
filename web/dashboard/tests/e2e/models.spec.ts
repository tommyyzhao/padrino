import { expect, test } from '@playwright/test';

const CATALOG_HASH = 'a'.repeat(64);

type CatalogItem = {
  canonical_slug: string;
  provider: string;
  display_name: string;
  description: string | null;
  context_length: number | null;
  pricing_per_million_micros: Record<string, number>;
  supported_capabilities: string[];
  supports_structured_output: boolean;
  qualification_tier: 'unverified' | 'playable' | 'verified_structured' | 'ranked_qualified';
  qualification_reasons: string[];
  casual_visible: boolean;
  privacy: 'zdr' | 'standard' | 'unknown';
  availability: 'available' | 'unavailable' | 'unknown' | 'stale' | 'deprecated';
  availability_reason: string;
  platform_eligible: boolean;
  platform_ineligibility_reasons: string[];
  observed_latency_ms?: number | null;
  observed_throughput_tokens_per_second?: number | null;
  open_weights?: boolean | null;
  frontier?: boolean | null;
  requires_byok?: boolean;
  catalog_freshness: {
    content_hash: string | null;
    fetched_at: string | null;
    state: 'current' | 'stale' | 'unavailable';
    is_stale: boolean;
  };
};

function model(
  canonicalSlug: string,
  overrides: Partial<CatalogItem> = {}
): CatalogItem {
  return {
    canonical_slug: canonicalSlug,
    provider: canonicalSlug.split('/')[0] ?? 'unknown',
    display_name: canonicalSlug,
    description: `A safe public description for ${canonicalSlug}.`,
    context_length: 128_000,
    pricing_per_million_micros: {
      prompt_per_million: 300_000,
      completion_per_million: 1_200_000
    },
    supported_capabilities: ['response_format', 'temperature'],
    supports_structured_output: true,
    qualification_tier: 'verified_structured',
    qualification_reasons: [],
    casual_visible: true,
    privacy: 'zdr',
    availability: 'available',
    availability_reason: 'source_available',
    platform_eligible: true,
    platform_ineligibility_reasons: [],
    observed_latency_ms: 840,
    observed_throughput_tokens_per_second: 96,
    open_weights: false,
    frontier: true,
    requires_byok: false,
    catalog_freshness: {
      content_hash: CATALOG_HASH,
      fetched_at: '2026-07-12T12:00:00Z',
      state: 'current',
      is_stale: false
    },
    ...overrides
  };
}

async function routeCatalog(
  page: import('@playwright/test').Page,
  items: CatalogItem[],
  state: 'current' | 'stale' | 'unavailable' = 'current'
) {
  await page.route('**/human/models*', async (route) => {
    const type = route.request().resourceType();
    if (type !== 'fetch' && type !== 'xhr') return route.continue();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        items,
        next_cursor: null,
        catalog: {
          content_hash: state === 'unavailable' ? null : CATALOG_HASH,
          fetched_at: state === 'unavailable' ? null : '2026-07-12T12:00:00Z',
          state,
          is_stale: state !== 'current'
        }
      })
    });
  });
}

test.describe('OpenRouter model explorer (US-366)', () => {
  test('searches and filters virtualized cards, compares three models, and selects canonical slugs safely', async ({
    page
  }) => {
    await routeCatalog(page, [
      model('openai/atlas'),
      model('mistral/ember', { open_weights: true, frontier: false }),
      model('anthropic/sonnet', { privacy: 'standard', observed_latency_ms: 1_900 }),
      model('community/unverified', {
        qualification_tier: 'unverified',
        qualification_reasons: ['probe_evidence_missing'],
        platform_eligible: false,
        platform_ineligibility_reasons: ['qualification_unverified']
      }),
      model('deepseek/unpriced', {
        pricing_per_million_micros: {},
        requires_byok: true,
        platform_eligible: false,
        platform_ineligibility_reasons: ['price_unavailable']
      })
    ]);

    await page.goto('/models');
    await expect(page.getByTestId('model-explorer-title')).toBeVisible();
    await expect(page.getByTestId('model-virtual-list')).toContainText('openai/atlas');
    await expect(page.getByTestId('model-card').first()).toContainText(/context|per million|estimated/i);
    await expect(page.getByTestId('model-card').first()).toContainText(/latency|throughput/i);

    await page.getByLabel('Search models').fill('mistral');
    await expect(page.getByTestId('model-virtual-list')).toContainText('mistral/ember');
    await expect(page.getByTestId('model-virtual-list')).not.toContainText('openai/atlas');
    await page.getByLabel('Open-weight filter').check();
    await expect(page.getByTestId('model-virtual-list')).toContainText('mistral/ember');

    await page.getByLabel('Search models').fill('');
    await page.getByLabel('Open-weight filter').uncheck();
    await page.getByLabel('Structured output only').check();
    await page.getByLabel('Ranked-qualified preview').check();
    await expect(page.getByTestId('model-empty-state')).toBeVisible();
    await page.getByLabel('Ranked-qualified preview').uncheck();

    for (const slug of ['openai/atlas', 'mistral/ember', 'anthropic/sonnet']) {
      await page.getByTestId(`model-compare-${slug}`).click();
    }
    await expect(page.getByTestId('model-comparison')).toBeVisible();
    await expect(page.getByTestId('model-comparison')).toContainText('openai/atlas');
    await expect(page.getByTestId('model-comparison')).toContainText('mistral/ember');
    await expect(page.getByTestId('model-comparison')).toContainText('anthropic/sonnet');

    await page.getByTestId('model-select-community/unverified').click();
    await expect(page.getByTestId('model-reliability-confirmation')).toBeVisible();
    await page.getByTestId('model-reliability-continue').click();
    await expect(page.getByTestId('model-selection')).toContainText('community/unverified');

    await page.getByTestId('model-select-deepseek/unpriced').click();
    await expect(page.getByTestId('model-byok-confirmation')).toBeVisible();
    await page.getByLabel('I understand this model needs my connected OpenRouter account').check();
    await page.getByTestId('model-byok-continue').click();
    await expect(page.getByTestId('model-selection')).toContainText('deepseek/unpriced');
    await expect(page.getByTestId('model-selection')).not.toContainText(/agentbuild|modelconfig|uuid/i);
  });

  test('explains stale and unavailable catalog states without exposing raw service errors', async ({ page }) => {
    await routeCatalog(page, [model('openai/stale')], 'stale');
    await page.goto('/models');
    await expect(page.getByTestId('model-catalog-stale')).toContainText(/stale|refresh/i);

    await page.unroute('**/human/models*');
    await page.route('**/human/models*', async (route) => {
      const type = route.request().resourceType();
      if (type !== 'fetch' && type !== 'xhr') return route.continue();
      await route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"secret upstream failure"}' });
    });
    await page.getByTestId('model-catalog-retry').click();
    await expect(page.getByTestId('model-catalog-error')).toContainText(/could not be reached|try again/i);
    await expect(page.getByTestId('model-catalog-error')).not.toContainText('secret upstream failure');
  });
});
