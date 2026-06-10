import { expect, test } from '@playwright/test';

// US-092: Live/recent lobby page.
//
// Both /public/live and /public/recent are intercepted via page.route so these
// tests run without a live backend with broadcast-state data.  Two scenarios:
//   1. Live card renders phase / players_alive / watch link and NO outcome.
//   2. Recent card renders winner and NO live-state badge.

const LIVE_GAME_ID = 'aaaa0001-0001-0001-0001-aaaaaaaaaaaa';
const RECENT_GAME_ID = 'bbbb0002-0002-0002-0002-bbbbbbbbbbbb';

test.describe('lobby', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('**/public/live*', async (route) => {
      if (route.request().resourceType() === 'fetch' || route.request().resourceType() === 'xhr') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            items: [
              {
                game_id: LIVE_GAME_ID,
                ruleset_id: 'mini7_v1',
                current_phase: 'Day 1',
                players_alive: 5
              }
            ],
            total: 1
          })
        });
      } else {
        await route.continue();
      }
    });

    await page.route('**/public/recent*', async (route) => {
      if (route.request().resourceType() === 'fetch' || route.request().resourceType() === 'xhr') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            items: [
              {
                game_id: RECENT_GAME_ID,
                ruleset_id: 'mini7_v1',
                current_phase: null,
                terminal_result: { winner: 'TOWN', reason: 'all_mafia_eliminated' }
              }
            ],
            next_cursor: null,
            total_estimate: 1
          })
        });
      } else {
        await route.continue();
      }
    });
  });

  test('live card renders phase and players alive; no outcome shown (spoiler safe)', async ({
    page
  }) => {
    await page.goto('/');

    // Live section is present
    await expect(page.getByTestId('lobby-live-section')).toBeVisible();

    // Wait for the card to appear (data loads asynchronously)
    const liveCard = page.getByTestId('lobby-live-card').first();
    await expect(liveCard).toBeVisible({ timeout: 15_000 });

    // Phase and players alive rendered on the live card
    await expect(page.getByTestId('lobby-live-phase').first()).toHaveText('Day 1');
    await expect(page.getByTestId('lobby-live-players').first()).toContainText('5');

    // Spoiler safety: no winner shown inside the live section (recent section may have one)
    const liveSection = page.getByTestId('lobby-live-section');
    await expect(liveSection.getByTestId('lobby-recent-winner')).toHaveCount(0);

    // Card links to the watch page for this game
    await expect(liveCard).toHaveAttribute('href', `/watch/${LIVE_GAME_ID}`);
  });

  test('recent card renders winner and links to watch page', async ({ page }) => {
    await page.goto('/');

    // Recent section is present
    await expect(page.getByTestId('lobby-recent-section')).toBeVisible();

    // Wait for the card to appear
    const recentCard = page.getByTestId('lobby-recent-card').first();
    await expect(recentCard).toBeVisible({ timeout: 15_000 });

    // Winner is exposed on the recent card
    await expect(page.getByTestId('lobby-recent-winner').first()).toContainText('TOWN');

    // Card links to the watch page for this game
    await expect(recentCard).toHaveAttribute('href', `/watch/${RECENT_GAME_ID}`);
  });
});
