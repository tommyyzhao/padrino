import { expect, test } from '@playwright/test';

// US-101: Ladder + model profile pages.
//
// /public/rulesets and /public/ladder are intercepted via page.route so these
// tests run without a live backend. Three scenarios:
//   1. Ranked table renders with ordinal scores and agent links.
//   2. Provisional badge appears for agents below threshold.
//   3. Ruleset switch is driven by canonical ruleset metadata and reloads.

const BUILD_ID_A = 'aaaaaaaa-0001-0001-0001-000000000001';
const BUILD_ID_B = 'bbbbbbbb-0002-0002-0002-000000000002';
const BUILD_ID_C = 'cccccccc-0003-0003-0003-000000000003';

const RULESET_OPTIONS = [
  {
    ruleset_id: 'mini7_v1',
    label: 'Mini 7 canonical team',
    player_count: 7,
    rating_context_kind: 'CANONICAL_TEAM',
    is_canonical: true
  },
  {
    ruleset_id: 'bench10_v1',
    label: 'Bench 10 canonical team',
    player_count: 10,
    rating_context_kind: 'CANONICAL_TEAM',
    is_canonical: true
  },
  {
    ruleset_id: 'sk12_v1',
    label: 'Serial Killer placement',
    player_count: 12,
    rating_context_kind: 'PLACEMENT',
    is_canonical: false
  }
];

const MINI7_LADDER_ENTRIES = [
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
    await page.route('**/public/rulesets', async (route) => {
      if (
        route.request().resourceType() === 'fetch' ||
        route.request().resourceType() === 'xhr'
      ) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ items: RULESET_OPTIONS })
        });
      } else {
        await route.continue();
      }
    });

    await page.route('**/public/ladder*', async (route) => {
      if (
        route.request().resourceType() === 'fetch' ||
        route.request().resourceType() === 'xhr'
      ) {
        const url = new URL(route.request().url());
        const rulesetId = url.searchParams.get('ruleset_id');
        const entries =
          rulesetId === 'bench10_v1'
            ? [
                {
                  agent_build_id: BUILD_ID_C,
                  display_name: 'BenchBot',
                  version: 'v2',
                  ordinal: 1330,
                  provisional: false,
                  games: 42,
                  last_game_at: '2026-06-10T00:00:00Z'
                }
              ]
            : MINI7_LADDER_ENTRIES;
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            ruleset_id: rulesetId ?? 'mini7_v1',
            entries,
            next_cursor: null,
            total_estimate: entries.length
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

  test('ruleset switch lists canonical team rulesets only and reloads selected standings', async ({
    page
  }) => {
    await page.goto('/ladder');

    const switcher = page.getByTestId('ladder-ruleset-switch');
    await expect(switcher).toBeVisible();

    const mini7 = page.getByTestId('ladder-ruleset-btn').filter({ hasText: 'mini7_v1' });
    const bench10 = page.getByTestId('ladder-ruleset-btn').filter({ hasText: 'bench10_v1' });
    await expect(mini7).toBeVisible();
    await expect(bench10).toBeVisible();
    await expect(page.getByTestId('ladder-ruleset-btn').filter({ hasText: 'sk12_v1' })).toHaveCount(
      0
    );
    await expect(mini7).toHaveAttribute('data-active', 'true');

    await bench10.click();
    await expect(bench10).toHaveAttribute('data-active', 'true');
    await expect(page.getByTestId('ladder-row')).toHaveCount(1);
    await expect(page.getByTestId('ladder-agent-link')).toHaveText('BenchBot');
    await expect(page.getByTestId('ladder-ordinal')).toHaveText('1330');
  });
});

test.describe('model detail', () => {
  test('falls back when rulesets are unavailable', async ({ page }) => {
    const requestedRulesets: string[] = [];

    await page.route('**/public/rulesets', async (route) => {
      if (
        route.request().resourceType() === 'fetch' ||
        route.request().resourceType() === 'xhr'
      ) {
        await route.fulfill({
          status: 500,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'rulesets unavailable' })
        });
      } else {
        await route.continue();
      }
    });

    await page.route('**/public/ladder*', async (route) => {
      if (
        route.request().resourceType() === 'fetch' ||
        route.request().resourceType() === 'xhr'
      ) {
        const url = new URL(route.request().url());
        const rulesetId = url.searchParams.get('ruleset_id') ?? '';
        requestedRulesets.push(rulesetId);
        const entries =
          rulesetId === 'bench10_v1'
            ? [
                {
                  agent_build_id: BUILD_ID_C,
                  display_name: 'BenchBot',
                  version: 'v2',
                  ordinal: 1330,
                  provisional: false,
                  games: 42,
                  last_game_at: '2026-06-10T00:00:00Z'
                }
              ]
            : [];
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            ruleset_id: rulesetId,
            entries,
            next_cursor: null,
            total_estimate: entries.length
          })
        });
      } else {
        await route.continue();
      }
    });

    await page.route('**/public/recent*', async (route) => {
      if (
        route.request().resourceType() === 'fetch' ||
        route.request().resourceType() === 'xhr'
      ) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ items: [], next_cursor: null, total_estimate: 0 })
        });
      } else {
        await route.continue();
      }
    });

    await page.route('**/public/models/*/analytics', async (route) => {
      if (
        route.request().resourceType() === 'fetch' ||
        route.request().resourceType() === 'xhr'
      ) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            agent_build_id: BUILD_ID_C,
            ruleset_id: 'bench10_v1',
            version: 'v2',
            games_played: 42,
            role_win_rates: [],
            voting_accuracy: { total_votes: 0, accurate_votes: 0, rate: 0 },
            survival_curve: [],
            computed_at: '2026-06-10T00:00:00Z'
          })
        });
      } else {
        await route.continue();
      }
    });

    await page.goto(`/models/${BUILD_ID_C}`);

    await expect(page.getByTestId('model-display-name')).toHaveText('BenchBot');
    await expect(page.getByTestId('model-error')).toHaveCount(0);
    expect(requestedRulesets).toContain('bench10_v1');
  });
});
