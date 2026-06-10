import { expect, test } from '@playwright/test';

// US-101: Ladder + model profile pages.
//
// /public/ladder is intercepted via page.route so these tests run without a
// live backend. Three scenarios:
//   1. Ranked table renders with ordinal scores and agent links.
//   2. Provisional badge appears for agents below threshold.
//   3. Ruleset switch renders and is interactive.

const BUILD_ID_A = 'aaaaaaaa-0001-0001-0001-000000000001';
const BUILD_ID_B = 'bbbbbbbb-0002-0002-0002-000000000002';

const LADDER_ENTRIES = [
  {
    agent_build_id: BUILD_ID_A,
    display_name: 'AlphaBot',
    version: 'v1',
    ordinal: 1250,
    provisional: false,
    games: 20,
    last_game_at: '2026-06-01T00:00:00Z'
  },
  {
    agent_build_id: BUILD_ID_B,
    display_name: 'BetaBot',
    version: 'v1',
    ordinal: 1050,
    provisional: true,
    games: 3,
    last_game_at: null
  }
];

test.describe('ladder', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('**/public/ladder*', async (route) => {
      if (
        route.request().resourceType() === 'fetch' ||
        route.request().resourceType() === 'xhr'
      ) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            ruleset_id: 'mini7_v1',
            entries: LADDER_ENTRIES,
            next_cursor: null,
            total_estimate: 2
          })
        });
      } else {
        await route.continue();
      }
    });
  });

  test('ranking table renders with ordinal scores and agent links', async ({ page }) => {
    await page.goto('/ladder');

    const table = page.getByTestId('ladder-table');
    await expect(table).toBeVisible({ timeout: 15_000 });

    // Both rows rendered
    await expect(page.getByTestId('ladder-row')).toHaveCount(2);

    // First row is AlphaBot with ordinal 1250
    const firstRow = page.getByTestId('ladder-row').first();
    await expect(firstRow.getByTestId('ladder-ordinal')).toHaveText('1250');
    await expect(firstRow.getByTestId('ladder-agent-link')).toHaveText('AlphaBot');
    await expect(firstRow.getByTestId('ladder-agent-link')).toHaveAttribute(
      'href',
      `/models/${BUILD_ID_A}`
    );

    // Second row is BetaBot with ordinal 1050
    const secondRow = page.getByTestId('ladder-row').nth(1);
    await expect(secondRow.getByTestId('ladder-ordinal')).toHaveText('1050');
    await expect(secondRow.getByTestId('ladder-agent-link')).toHaveText('BetaBot');
  });

  test('provisional badge appears for provisional agents, established badge for others', async ({
    page
  }) => {
    await page.goto('/ladder');

    await expect(page.getByTestId('ladder-table')).toBeVisible({ timeout: 15_000 });

    // AlphaBot is established
    const firstRow = page.getByTestId('ladder-row').first();
    await expect(firstRow.getByTestId('ladder-established-badge')).toBeVisible();
    await expect(firstRow.getByTestId('ladder-provisional-badge')).toHaveCount(0);

    // BetaBot is provisional
    const secondRow = page.getByTestId('ladder-row').nth(1);
    await expect(secondRow.getByTestId('ladder-provisional-badge')).toBeVisible();
    await expect(secondRow.getByTestId('ladder-established-badge')).toHaveCount(0);
  });

  test('ruleset switch renders and marks active ruleset', async ({ page }) => {
    await page.goto('/ladder');

    const switcher = page.getByTestId('ladder-ruleset-switch');
    await expect(switcher).toBeVisible();

    // mini7_v1 button is present and active
    const btn = page.getByTestId('ladder-ruleset-btn').filter({ hasText: 'mini7_v1' });
    await expect(btn).toBeVisible();
    await expect(btn).toHaveAttribute('data-active', 'true');
  });
});
