import { expect, test, type Page, type Route } from '@playwright/test';
import { expectIdentityBlind } from './helpers/identityBlind';

// US-070 / US-111 / US-118 / US-186 / US-229: the leaderboard is a
// public-surface-only page sourced exclusively from `/public/leaderboard`.
// The spec is hermetic and intercepts public fetches so navigation from the
// home page does not depend on smoke-harness data.

const BASE_CARD = {
  card_id: 'card-default',
  section: 'canonical',
  section_label: 'Ranked canonical',
  context_kind: 'CANONICAL_TEAM',
  context_label: 'Bench 10 canonical team',
  ruleset_id: 'bench10_v1',
  entity_id: 'entity-default',
  display_name: 'Default',
  model_provider: 'mock',
  model_name: 'mock-model',
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
};

function card(overrides: Record<string, unknown>) {
  return { ...BASE_CARD, ...overrides };
}

const LEADERBOARD = {
  ruleset_id: null,
  gauntlet_id: null,
  rating_model: 'openskill_pl_v1',
  cache_tag: 'seed-tag',
  entries: [],
  canonical_cards: [
    card({
      card_id: 'card-canonical-alpha',
      entity_id: 'entity-alpha',
      display_name: 'Alpha',
      model_name: 'mock-a',
      score: 31.2,
      rank: 1,
      sample_count: 12,
      games: 12,
      mu: 37.5,
      sigma: 2.1,
      conservative_score: 31.2
    }),
    card({
      card_id: 'card-canonical-bravo',
      entity_id: 'entity-bravo',
      display_name: 'Bravo',
      model_name: 'mock-b',
      score: 40.0,
      rank: null,
      provisional: true,
      provisional_reason: 'Requires at least 10 games in this context; current sample is 2',
      sample_count: 2,
      games: 2,
      mu: 43.0,
      sigma: 1.0,
      conservative_score: 40.0
    })
  ],
  faction_cards: [
    card({
      card_id: 'card-faction-town',
      entity_id: 'entity-atlas-town',
      display_name: 'Atlas',
      scope_type: 'FACTION',
      scope_value: 'TOWN',
      score: 28.8,
      rank: 1,
      sample_count: 18,
      games: 18,
      mu: 34.2,
      sigma: 1.8,
      conservative_score: 28.8
    }),
    card({
      card_id: 'card-faction-scum',
      entity_id: 'entity-atlas-scum',
      display_name: 'Atlas',
      scope_type: 'FACTION',
      scope_value: 'MAFIA',
      score: 27.1,
      rank: 1,
      sample_count: 9,
      games: 9,
      mu: 33.1,
      sigma: 2.0,
      conservative_score: 27.1
    })
  ],
  experimental_cards: [
    card({
      card_id: 'card-placement',
      section: 'experimental',
      section_label: 'Experimental context',
      context_kind: 'PLACEMENT',
      context_label: 'Serial Killer 12 placement',
      ruleset_id: 'sk12_v1',
      entity_id: 'entity-charlie',
      display_name: 'Charlie',
      model_name: 'mock-c',
      metric_label: 'Placement rating',
      score: 24.8,
      rank: 1,
      sample_count: 14,
      games: 14,
      mu: 39.5,
      sigma: 4.9,
      conservative_score: 24.8
    }),
    card({
      card_id: 'card-solo',
      section: 'experimental',
      section_label: 'Experimental context',
      context_kind: 'SOLO_RATE',
      context_label: 'Jester 8 lynch-bait',
      ruleset_id: 'jester8_v1',
      entity_id: 'entity-delta',
      display_name: 'Delta',
      model_name: 'mock-d',
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
    })
  ],
  human_cards: [
    card({
      card_id: 'card-human-ace',
      section: 'humans_included',
      section_label: 'Humans-Included League',
      context_kind: 'HUMANS_INCLUDED',
      context_label: 'Humans-Included mini7_v1 ranked',
      ruleset_id: 'mini7_v1',
      entity_id: 'entity-human-ace',
      display_name: 'Human Ace',
      model_provider: 'human',
      model_name: 'human_player',
      prompt_version: 'humans-included',
      metric_label: 'Human ELO',
      score: 28.0,
      rank: 1,
      sample_count: 12,
      games: 12,
      mu: 34.0,
      sigma: 2.0,
      conservative_score: 28.0
    })
  ],
  next_cursor: null,
  total_estimate: 0
};

async function fulfillJson(route: Route, body: unknown) {
  await route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(body)
  });
}

function mockPublicSurfaces(page: Page) {
  return page.route('**/public/**', async (route) => {
    const request = route.request();
    if (request.resourceType() !== 'fetch' && request.resourceType() !== 'xhr') {
      await route.continue();
      return;
    }

    const url = new URL(request.url());
    if (url.pathname === '/public/leaderboard') {
      await fulfillJson(route, LEADERBOARD);
      return;
    }
    if (url.pathname === '/public/live') {
      await fulfillJson(route, { items: [], total: 0 });
      return;
    }
    if (url.pathname === '/public/recent') {
      await fulfillJson(route, { items: [], next_cursor: null, total_estimate: 0 });
      return;
    }
    if (url.pathname === '/public/ladder') {
      await fulfillJson(route, {
        ruleset_id: 'mini7_v1',
        entries: [],
        next_cursor: null,
        total_estimate: 0
      });
      return;
    }
    if (url.pathname === '/public/rulesets') {
      await fulfillJson(route, {
        items: [
          {
            ruleset_id: 'mini7_v1',
            label: 'Mini 7 canonical team',
            player_count: 7,
            rating_context_kind: 'CANONICAL_TEAM',
            is_canonical: true
          }
        ]
      });
      return;
    }

    await route.continue();
  });
}

async function expectLeaderboardSections(page: Page) {
  await expect(page.getByTestId('leaderboard-title')).toBeVisible();
  await expect(page.getByTestId('leaderboard-loading')).toHaveCount(0, { timeout: 30_000 });
  await expect(page.getByTestId('leaderboard-canonical-section')).toBeVisible();
  await expect(page.getByTestId('leaderboard-canonical-global-subsection')).toBeVisible();
  await expect(page.getByTestId('leaderboard-canonical-faction-subsection')).toBeVisible();
  await expect(page.getByTestId('leaderboard-humans-included-section')).toBeVisible();
  await expect(page.getByTestId('leaderboard-placement-section')).toBeVisible();
  await expect(page.getByTestId('leaderboard-solo-rate-section')).toBeVisible();
}

test.describe('leaderboard', () => {
  test('renders canonical, faction, placement, and solo-rate cards without a merged ranking', async ({
    page
  }) => {
    await mockPublicSurfaces(page);
    await page.goto('/leaderboard');

    await expectLeaderboardSections(page);

    const globalCards = page.locator(
      '[data-testid="leaderboard-card"][data-subsection="canonical-global"]'
    );
    const factionCards = page.locator(
      '[data-testid="leaderboard-card"][data-subsection="canonical-faction"]'
    );
    const placementCards = page.locator(
      '[data-testid="leaderboard-card"][data-subsection="placement"]'
    );
    const humanCards = page.locator(
      '[data-testid="leaderboard-card"][data-subsection="humans-included"]'
    );
    const soloRateCards = page.locator(
      '[data-testid="leaderboard-card"][data-subsection="solo-rate"]'
    );

    await expect(globalCards).toHaveCount(2);
    await expect(factionCards).toHaveCount(2);
    await expect(humanCards).toHaveCount(1);
    await expect(placementCards).toHaveCount(1);
    await expect(soloRateCards).toHaveCount(1);
    await expect(globalCards.first().getByTestId('leaderboard-card-rank')).toHaveText('#1');
    await expect(globalCards.nth(1).getByTestId('leaderboard-card-rank')).toHaveText(
      'Provisional'
    );
    await expect(factionCards.first().getByTestId('leaderboard-card-scope')).toHaveText(
      'Faction: Town'
    );
    await expect(factionCards.nth(1).getByTestId('leaderboard-card-scope')).toHaveText(
      'Faction: Scum'
    );
    await expect(placementCards.first().getByTestId('leaderboard-card-rank')).toHaveText('#1');
    await expect(humanCards.first()).toContainText('Human ELO');
    await expect(humanCards.first().getByTestId('leaderboard-card-name')).toHaveText('Human Ace');
    await expect(soloRateCards.first()).toContainText('CI 35-49%');
    await expect(page.getByTestId('leaderboard-card-provisional').first()).toContainText(
      'Under-sampled'
    );
    await expectIdentityBlind(page.locator('body'));
  });

  test('is reachable from the home page and the main nav', async ({ page }) => {
    await mockPublicSurfaces(page);
    await page.goto('/');

    await page.getByTestId('home-leaderboard-link').click();
    await expectLeaderboardSections(page);

    await page.goto('/');
    await page.getByTestId('nav-leaderboard').click();
    await expectLeaderboardSections(page);
  });

  test('matches the leaderboard visual snapshot', async ({ page }) => {
    test.skip(!process.env.PADRINO_E2E_VISUAL, 'visual regression opt-in only');
    await mockPublicSurfaces(page);
    await page.goto('/leaderboard');
    await expect(page.getByTestId('leaderboard-loading')).toHaveCount(0, { timeout: 30_000 });
    await expect(page).toHaveScreenshot('leaderboard.png', {
      fullPage: true,
      mask: [page.getByTestId('leaderboard-card')]
    });
  });
});
