import { expect, test } from '@playwright/test';

// US-106: Post-match breakdown + analytics cards.
//
// The /public/games/{id}/analytics endpoint is intercepted via page.route so
// these tests run without a live backend.  Three scenarios:
//   1. RECENT game -> full analytics rendered (winner, voting accuracy, role win rates).
//   2. LIVE game (winner=null, role_win_rates=null) -> "not available yet" notice shown.
//   3. Unknown game (404) -> not-found message shown.

const RECENT_GAME_ID = 'aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa';
const LIVE_GAME_ID = 'bbbbbbbb-2222-2222-2222-bbbbbbbbbbbb';
const UNKNOWN_GAME_ID = 'cccccccc-3333-3333-3333-cccccccccccc';

const RECENT_ANALYTICS = {
  game_id: RECENT_GAME_ID,
  ruleset_id: 'mini7_v1',
  winner: 'TOWN',
  voting_accuracy: { total_votes: 10, accurate_votes: 4, rate: 0.4 },
  survival_curve: [
    { role: 'Villager', day: 1, alive_count: 3, total_count: 3, fraction: 1.0 },
    { role: 'Villager', day: 2, alive_count: 2, total_count: 3, fraction: 0.667 }
  ],
  role_win_rates: [
    { role: 'Villager', wins: 3, games: 4, rate: 0.75 },
    { role: 'Mafia', wins: 1, games: 4, rate: 0.25 }
  ],
  claims: [
    {
      player_id: 'aaaaaaaa-bbbb-cccc-dddd-ee0000000000',
      claimed_role: 'Detective',
      sequence: 5,
      phase: 'Day 1'
    }
  ],
  counter_claims: []
};

const LIVE_ANALYTICS = {
  game_id: LIVE_GAME_ID,
  ruleset_id: 'mini7_v1',
  winner: null,
  voting_accuracy: { total_votes: 3, accurate_votes: 1, rate: 0.333 },
  survival_curve: [],
  role_win_rates: null,
  claims: [],
  counter_claims: []
};

function mockAnalytics(
  page: import('@playwright/test').Page,
  gameId: string,
  body: unknown,
  status = 200
) {
  return page.route(`**/public/games/${gameId}/analytics*`, async (route) => {
    if (
      route.request().resourceType() === 'fetch' ||
      route.request().resourceType() === 'xhr'
    ) {
      await route.fulfill({
        status,
        contentType: 'application/json',
        body: JSON.stringify(body)
      });
    } else {
      await route.continue();
    }
  });
}

test.describe('recap', () => {
  test('RECENT game: deterministic stats render with winner and role win rates', async ({
    page
  }) => {
    await mockAnalytics(page, RECENT_GAME_ID, RECENT_ANALYTICS);

    await page.goto(`/watch/${RECENT_GAME_ID}/recap`);
    await expect(page.getByTestId('recap-title')).toBeVisible();

    // No live notice
    await expect(page.getByTestId('recap-live-notice')).toHaveCount(0);

    // Winner shown
    await expect(page.getByTestId('recap-winner')).toHaveText('TOWN');

    // Voting accuracy
    await expect(page.getByTestId('recap-voting-accuracy')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('recap-total-votes')).toHaveText('10');
    await expect(page.getByTestId('recap-accurate-votes')).toHaveText('4');
    await expect(page.getByTestId('recap-vote-rate')).toHaveText('40.0%');

    // Role win rates
    await expect(page.getByTestId('recap-role-win-rates')).toBeVisible();
    await expect(page.getByTestId('recap-role-win-rate-row')).toHaveCount(2);

    // Survival curve
    await expect(page.getByTestId('recap-survival-curve')).toBeVisible();

    // Claims section
    await expect(page.getByTestId('recap-claims')).toBeVisible();
    await expect(page.getByTestId('recap-claim-row')).toHaveCount(1);
  });

  test('LIVE game: shows "not yet available" notice instead of spoilers', async ({ page }) => {
    await mockAnalytics(page, LIVE_GAME_ID, LIVE_ANALYTICS);

    await page.goto(`/watch/${LIVE_GAME_ID}/recap`);
    await expect(page.getByTestId('recap-title')).toBeVisible();

    // Live notice is visible
    await expect(page.getByTestId('recap-live-notice')).toBeVisible({ timeout: 15_000 });

    // Winner / role win rates NOT shown (spoiler safe)
    await expect(page.getByTestId('recap-winner')).toHaveCount(0);
    await expect(page.getByTestId('recap-role-win-rates')).toHaveCount(0);
  });

  test('unknown game (404): shows not-found message', async ({ page }) => {
    await mockAnalytics(
      page,
      UNKNOWN_GAME_ID,
      { detail: 'game_not_found_or_not_broadcastable' },
      404
    );

    await page.goto(`/watch/${UNKNOWN_GAME_ID}/recap`);
    await expect(page.getByTestId('recap-title')).toBeVisible();
    await expect(page.getByTestId('recap-not-found')).toBeVisible({ timeout: 15_000 });
  });
});
