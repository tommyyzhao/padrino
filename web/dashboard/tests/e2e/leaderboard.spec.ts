import { expect, test } from '@playwright/test';

// US-070 / US-111 / US-118 / US-186: the leaderboard is a
// public-surface-only page sourced exclusively from `/public/leaderboard`.
// US-186 separates canonical cards from experimental context cards so the UI
// never presents one cross-context ranking.
//
// US-118 (flake burn-down): the spec is now HERMETIC. It no longer depends on
// the smoke harness having a verified ingested game in the federated rollup
// (post-US-112 the public leaderboard excludes unverified ingests, so the
// presence of rows is state-dependent). `/public/leaderboard` is intercepted
// via `page.route` with deterministic seeded entries so rows always render.

const LEADERBOARD = {
  ruleset_id: null,
  gauntlet_id: null,
  rating_model: 'openskill_pl_v1',
  cache_tag: 'seed-tag',
  entries: [],
  canonical_cards: [
    {
      card_id: 'card-canonical-alpha',
      section: 'canonical',
      section_label: 'Ranked canonical',
      context_kind: 'CANONICAL_TEAM',
      context_label: 'Bench 10 canonical team',
      ruleset_id: 'bench10_v1',
      entity_id: 'entity-alpha',
      display_name: 'Alpha',
      model_provider: 'mock',
      model_name: 'mock-a',
      model_version: null,
      prompt_version: 'v1',
      scope_type: 'GLOBAL',
      scope_value: 'global',
      metric: 'openskill_conservative',
      metric_label: 'Canonical ELO',
      score: 31.2,
      rank: 1,
      provisional: false,
      provisional_reason: null,
      sample_count: 12,
      games: 12,
      attempts: null,
      successes: null,
      mu: 37.5,
      sigma: 2.1,
      conservative_score: 31.2,
      mean_success_rate: null,
      credible_interval_low: null,
      credible_interval_high: null
    },
    {
      card_id: 'card-canonical-bravo',
      section: 'canonical',
      section_label: 'Ranked canonical',
      context_kind: 'CANONICAL_TEAM',
      context_label: 'Bench 10 canonical team',
      ruleset_id: 'bench10_v1',
      entity_id: 'entity-bravo',
      display_name: 'Bravo',
      model_provider: 'mock',
      model_name: 'mock-b',
      model_version: null,
      prompt_version: 'v1',
      scope_type: 'GLOBAL',
      scope_value: 'global',
      metric: 'openskill_conservative',
      metric_label: 'Canonical ELO',
      score: 40.0,
      rank: null,
      provisional: true,
      provisional_reason: 'Requires at least 10 games in this context; current sample is 2',
      sample_count: 2,
      games: 2,
      attempts: null,
      successes: null,
      mu: 43.0,
      sigma: 1.0,
      conservative_score: 40.0,
      mean_success_rate: null,
      credible_interval_low: null,
      credible_interval_high: null
    }
  ],
  experimental_cards: [
    {
      card_id: 'card-placement',
      section: 'experimental',
      section_label: 'Experimental context',
      context_kind: 'PLACEMENT',
      context_label: 'Serial Killer 12 placement',
      ruleset_id: 'sk12_v1',
      entity_id: 'entity-charlie',
      display_name: 'Charlie',
      model_provider: 'mock',
      model_name: 'mock-c',
      model_version: null,
      prompt_version: 'v1',
      scope_type: 'GLOBAL',
      scope_value: 'global',
      metric: 'openskill_conservative',
      metric_label: 'Placement rating',
      score: 24.8,
      rank: 1,
      provisional: false,
      provisional_reason: null,
      sample_count: 14,
      games: 14,
      attempts: null,
      successes: null,
      mu: 39.5,
      sigma: 4.9,
      conservative_score: 24.8,
      mean_success_rate: null,
      credible_interval_low: null,
      credible_interval_high: null
    },
    {
      card_id: 'card-solo',
      section: 'experimental',
      section_label: 'Experimental context',
      context_kind: 'SOLO_RATE',
      context_label: 'Jester 8 lynch-bait',
      ruleset_id: 'jester8_v1',
      entity_id: 'entity-delta',
      display_name: 'Delta',
      model_provider: 'mock',
      model_name: 'mock-d',
      model_version: null,
      prompt_version: 'v1',
      scope_type: 'ROLE',
      scope_value: 'JESTER',
      metric: 'solo_success_rate',
      metric_label: 'Solo success rate',
      score: 0.42,
      rank: null,
      provisional: true,
      provisional_reason: 'Requires at least 10 attempts in this context; current sample is 6',
      sample_count: 6,
      games: null,
      attempts: 6,
      successes: 3,
      mu: null,
      sigma: null,
      conservative_score: null,
      mean_success_rate: 0.42,
      credible_interval_low: 0.35,
      credible_interval_high: 0.49
    }
  ],
  next_cursor: null,
  total_estimate: 0
};

function mockLeaderboard(page: import('@playwright/test').Page) {
  return page.route('**/public/leaderboard*', async (route) => {
    if (route.request().resourceType() === 'fetch' || route.request().resourceType() === 'xhr') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(LEADERBOARD)
      });
    } else {
      await route.continue();
    }
  });
}

test.describe('leaderboard', () => {
  test('renders canonical and experimental cards without a merged ranking', async ({ page }) => {
    await mockLeaderboard(page);
    await page.goto('/leaderboard');

    await expect(page.getByTestId('leaderboard-title')).toBeVisible();
    await expect(page.getByTestId('leaderboard-loading')).toHaveCount(0, { timeout: 30_000 });

    await expect(page.getByTestId('leaderboard-canonical-section')).toBeVisible();
    await expect(page.getByTestId('leaderboard-experimental-section')).toBeVisible();

    const canonicalCards = page.locator('[data-testid="leaderboard-card"][data-section="canonical"]');
    const experimentalCards = page.locator(
      '[data-testid="leaderboard-card"][data-section="experimental"]'
    );
    await expect(canonicalCards).toHaveCount(2);
    await expect(experimentalCards).toHaveCount(2);
    await expect(canonicalCards.first().getByTestId('leaderboard-card-rank')).toHaveText('#1');
    await expect(canonicalCards.nth(1).getByTestId('leaderboard-card-rank')).toHaveText(
      'Provisional'
    );
    await expect(experimentalCards.filter({ hasText: 'Jester 8 lynch-bait' })).toContainText(
      'CI 35-49%'
    );
  });

  test('matches the leaderboard visual snapshot', async ({ page }) => {
    test.skip(!process.env.PADRINO_E2E_VISUAL, 'visual regression opt-in only');
    await mockLeaderboard(page);
    await page.goto('/leaderboard');
    await expect(page.getByTestId('leaderboard-loading')).toHaveCount(0, { timeout: 30_000 });
    await expect(page).toHaveScreenshot('leaderboard.png', {
      fullPage: true,
      mask: [page.getByTestId('leaderboard-card')]
    });
  });
});
