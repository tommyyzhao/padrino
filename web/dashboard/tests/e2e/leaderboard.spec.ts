import { expect, test } from '@playwright/test';

// US-070 / US-111 / US-118: the leaderboard is a public-surface-only page
// sourced exclusively from `/public/leaderboard` (anonymous openskill rollup by
// ruleset). The Town/Mafia/Global tabs are faction views over the same rollup,
// so every tab re-fetches `/public/leaderboard`.
//
// US-118 (flake burn-down): the spec is now HERMETIC. It no longer depends on
// the smoke harness having a verified ingested game in the federated rollup
// (post-US-112 the public leaderboard excludes unverified ingests, so the
// presence of rows is state-dependent). `/public/leaderboard` is intercepted
// via `page.route` with deterministic seeded entries so rows always render.

const LEADERBOARD = {
  ruleset_id: 'mini7_v1',
  gauntlet_id: null,
  rating_model: 'openskill_pl_v1',
  cache_tag: 'seed-tag',
  entries: [
    {
      entity_id: 'eeee0001-0001-0001-0001-eeeeeeeeeeee',
      display_name: 'Alpha',
      model_provider: 'mock',
      model_name: 'mock-a',
      model_version: null,
      prompt_version: 'v1',
      games: 12,
      wins: 7,
      draws: 1,
      losses: 4,
      mu: 27.5,
      sigma: 4.2,
      conservative_score: 14.9
    },
    {
      entity_id: 'eeee0002-0002-0002-0002-eeeeeeeeeeee',
      display_name: 'Bravo',
      model_provider: 'mock',
      model_name: 'mock-b',
      model_version: null,
      prompt_version: 'v1',
      games: 10,
      wins: 4,
      draws: 0,
      losses: 6,
      mu: 24.1,
      sigma: 5.0,
      conservative_score: 9.1
    }
  ],
  next_cursor: null,
  total_estimate: 2
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
  test('loads rows, switches Town/Mafia tabs, and exposes pagination', async ({ page }) => {
    await mockLeaderboard(page);
    await page.goto('/leaderboard');

    await expect(page.getByTestId('leaderboard-title')).toBeVisible();
    await expect(page.getByTestId('leaderboard-loading')).toHaveCount(0, { timeout: 30_000 });

    const rows = page.getByTestId('leaderboard-row');
    await expect(rows.first()).toBeVisible();
    await expect(rows).toHaveCount(2);
    await expect(page.getByTestId('leaderboard-table')).toHaveAttribute('data-tab', 'global');

    // Switch to Town tab
    await page.getByTestId('leaderboard-tab-town').click();
    await expect(page.getByTestId('leaderboard-loading')).toHaveCount(0, { timeout: 15_000 });
    await expect(page.getByTestId('leaderboard-table')).toHaveAttribute('data-tab', 'town');
    await expect(rows.first()).toBeVisible();

    // Switch to Mafia tab
    await page.getByTestId('leaderboard-tab-mafia').click();
    await expect(page.getByTestId('leaderboard-loading')).toHaveCount(0, { timeout: 15_000 });
    await expect(page.getByTestId('leaderboard-table')).toHaveAttribute('data-tab', 'mafia');
    await expect(rows.first()).toBeVisible();

    // Back to Global
    await page.getByTestId('leaderboard-tab-global').click();
    await expect(page.getByTestId('leaderboard-loading')).toHaveCount(0, { timeout: 15_000 });
    await expect(page.getByTestId('leaderboard-table')).toHaveAttribute('data-tab', 'global');

    // Pagination controls are present; with a single page next is disabled and
    // previous always starts disabled (no history yet).
    const nextBtn = page.getByTestId('leaderboard-next');
    const prevBtn = page.getByTestId('leaderboard-prev');
    await expect(nextBtn).toBeVisible();
    await expect(prevBtn).toBeVisible();
    await expect(prevBtn).toBeDisabled();
    await expect(nextBtn).toBeDisabled();
  });

  test('matches the leaderboard visual snapshot', async ({ page }) => {
    test.skip(!process.env.PADRINO_E2E_VISUAL, 'visual regression opt-in only');
    await mockLeaderboard(page);
    await page.goto('/leaderboard');
    await expect(page.getByTestId('leaderboard-loading')).toHaveCount(0, { timeout: 30_000 });
    await expect(page).toHaveScreenshot('leaderboard.png', {
      fullPage: true,
      mask: [page.getByTestId('leaderboard-table')]
    });
  });
});
