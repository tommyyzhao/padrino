import { expect, test } from '@playwright/test';

// US-077: minimal smoke that the gauntlet evaluation report page renders the
// faction win-rate bars, role-family table, and rating-delta chart against a
// known JSON payload. The route is intercepted with `page.route` so the spec
// does NOT depend on the smoke-harness having produced rating events for any
// specific gauntlet ID — the smoke gauntlet only exercises the mock adapter
// and may not populate every column the report shows.

const FIXTURE_GAUNTLET_ID = '11111111-2222-3333-4444-555555555555';

const FIXTURE_REPORT = {
  gauntlet_id: FIXTURE_GAUNTLET_ID,
  status: 'COMPLETED',
  ruleset_id: 'mini7_v1',
  clone_count: 3,
  games_total: 3,
  games_completed: 3,
  faction_win_counts: { TOWN: 1, MAFIA: 1, DRAW: 1 },
  faction_win_rates: [
    {
      faction: 'TOWN',
      wins: 1,
      games: 3,
      rate: { point: 0.3333, lower: 0.0628, upper: 0.7972 }
    },
    {
      faction: 'MAFIA',
      wins: 1,
      games: 3,
      rate: { point: 0.3333, lower: 0.0628, upper: 0.7972 }
    },
    {
      faction: 'DRAW',
      wins: 1,
      games: 3,
      rate: { point: 0.3333, lower: 0.0628, upper: 0.7972 }
    }
  ],
  role_family_breakdown: [
    {
      role_family: 'DECEPTIVE',
      games: 6,
      wins: 2,
      draws: 2,
      losses: 2,
      win_rate: { point: 0.3333, lower: 0.0972, upper: 0.7012 }
    },
    {
      role_family: 'INVESTIGATIVE',
      games: 3,
      wins: 1,
      draws: 1,
      losses: 1,
      win_rate: { point: 0.3333, lower: 0.0628, upper: 0.7972 }
    },
    {
      role_family: 'PROTECTIVE',
      games: 3,
      wins: 1,
      draws: 1,
      losses: 1,
      win_rate: { point: 0.3333, lower: 0.0628, upper: 0.7972 }
    },
    {
      role_family: 'VANILLA_TOWN',
      games: 9,
      wins: 3,
      draws: 3,
      losses: 3,
      win_rate: { point: 0.3333, lower: 0.1183, upper: 0.6489 }
    }
  ],
  average_days_to_terminal: 4.0,
  average_actions_per_seat: 0.2857,
  rating_deltas: [
    {
      agent_build_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
      scope_type: 'GLOBAL',
      scope_value: 'global',
      games_in_gauntlet: 3,
      pre_mu: 25.0,
      pre_sigma: 8.3333,
      post_mu: 25.5,
      post_sigma: 8.1,
      delta_mu: 0.5,
      delta_sigma: -0.2333
    }
  ]
};

test.describe('gauntlet report', () => {
  test('renders summary, faction bars, role-family table, and rating deltas', async ({ page }) => {
    await page.route(
      `**/public/gauntlets/${FIXTURE_GAUNTLET_ID}/report`,
      async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(FIXTURE_REPORT)
        });
      }
    );

    await page.goto(`/gauntlets/${FIXTURE_GAUNTLET_ID}/report`);

    await expect(page.getByTestId('gauntlet-report-title')).toBeVisible();
    await expect(page.getByTestId('gauntlet-report-loading')).toHaveCount(0, { timeout: 15_000 });

    const shell = page.getByTestId('gauntlet-report-shell');
    await expect(shell).toHaveAttribute('data-status', 'COMPLETED');

    await expect(page.getByTestId('gauntlet-report-status')).toHaveText('COMPLETED');
    await expect(page.getByTestId('gauntlet-report-games')).toContainText('3 / 3');

    const factionRows = page.getByTestId('gauntlet-report-faction-row');
    await expect(factionRows).toHaveCount(3);
    const factions = await factionRows.evaluateAll((rows) =>
      rows.map((r) => r.getAttribute('data-faction'))
    );
    expect(new Set(factions)).toEqual(new Set(['TOWN', 'MAFIA', 'DRAW']));

    const roleRows = page.getByTestId('gauntlet-report-role-family-row');
    await expect(roleRows).toHaveCount(4);
    const families = await roleRows.evaluateAll((rows) =>
      rows.map((r) => r.getAttribute('data-role-family'))
    );
    expect(new Set(families)).toEqual(
      new Set(['DECEPTIVE', 'INVESTIGATIVE', 'PROTECTIVE', 'VANILLA_TOWN'])
    );

    const deltas = page.getByTestId('gauntlet-report-rating-delta-row');
    await expect(deltas).toHaveCount(1);
    await expect(page.getByTestId('gauntlet-report-rating-delta-mu')).toContainText('+0.50');
  });
});
