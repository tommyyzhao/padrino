import { expect, test } from '@playwright/test';

const SOLO_GAME_ID = 'ffff0005-0005-0005-0005-ffffffffffff';

type Page = import('@playwright/test').Page;
type Route = import('@playwright/test').Route;

function isApi(route: Route): boolean {
  const t = route.request().resourceType();
  return t === 'fetch' || t === 'xhr';
}

async function routeQuietHome(page: Page) {
  await page.route('**/public/live*', async (route) => {
    if (!isApi(route)) return route.continue();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [], total: 0 })
    });
  });

  await page.route('**/public/recent*', async (route) => {
    if (!isApi(route)) return route.continue();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [], next_cursor: null, total_estimate: 0 })
    });
  });

  await page.route('**/public/rulesets', async (route) => {
    if (!isApi(route)) return route.continue();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        items: [
          {
            ruleset_id: 'mini7_v1',
            label: 'Mini 7 canonical team',
            player_count: 7,
            rating_context_kind: 'CANONICAL_TEAM',
            is_canonical: true
          }
        ]
      })
    });
  });

  await page.route('**/public/ladder*', async (route) => {
    if (!isApi(route)) return route.continue();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ruleset_id: 'mini7_v1', entries: [], next_cursor: null })
    });
  });
}

test.describe('home', () => {
  test('shows Play vs AI as the primary CTA and a distinct nav entry', async ({ page }) => {
    await routeQuietHome(page);
    await page.goto('/');

    const primary = page.getByTestId('home-play-vs-ai-cta');
    await expect(primary).toBeVisible();
    await expect(primary).toHaveClass(/bg-primary/);
    await expect(page.getByTestId('home-watch-link')).toBeVisible();
    await expect(page.getByTestId('home-leaderboard-link')).toBeVisible();
    await expect(page.getByTestId('nav-play-vs-ai')).toHaveText('Play vs AI');
    await expect(page.getByTestId('nav-lobby')).toHaveText('Play with friends');
  });

  test('fresh visitor accepts inline consent, starts a match, and reaches play', async ({
    page
  }) => {
    await routeQuietHome(page);

    let guestCreated = false;
    let consented = false;
    let matched = false;
    await page.route('**/human/me', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: guestCreated ? 200 : 401,
        contentType: 'application/json',
        body: JSON.stringify(
          guestCreated
            ? { principal_id: 'guest-1', kind: 'guest', display_name: null }
            : { detail: 'not_authenticated' }
        )
      });
    });

    await page.route('**/human/guest', async (route) => {
      if (!isApi(route)) return route.continue();
      guestCreated = true;
      await route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({ principal_id: 'guest-1', kind: 'guest', display_name: null })
      });
    });

    await page.route('**/human/consent', async (route) => {
      if (!isApi(route)) return route.continue();
      if (route.request().method() === 'POST') consented = true;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          consented,
          required_versions: { TOS: '1', PRIVACY: '1', AGE_GATE: '1' }
        })
      });
    });

    await page.route('**/human/match', async (route) => {
      if (!isApi(route)) return route.continue();
      matched = true;
      await new Promise((resolve) => setTimeout(resolve, 150));
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ game_id: SOLO_GAME_ID })
      });
    });

    await page.goto('/');
    await page.getByTestId('home-play-vs-ai-cta').click();
    await expect(page.getByTestId('home-consent-row')).toBeVisible({ timeout: 15_000 });
    await page.getByTestId('home-consent-accept').click();
    await expect(page.getByTestId('match-queue-screen')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('match-state')).toContainText('Preparing');
    await expect(page).toHaveURL(new RegExp(`/play/${SOLO_GAME_ID}$`), { timeout: 15_000 });
    expect(guestCreated).toBe(true);
    expect(consented).toBe(true);
    expect(matched).toBe(true);
  });

  test('match queue screen is cancelable and ignores a late match response', async ({ page }) => {
    await routeQuietHome(page);

    let matchCalls = 0;

    await page.route('**/human/me', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ principal_id: 'guest-cancel', kind: 'guest', display_name: null })
      });
    });

    await page.route('**/human/consent', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          consented: true,
          required_versions: { TOS: '1', PRIVACY: '1', AGE_GATE: '1' }
        })
      });
    });

    await page.route('**/human/match', async (route) => {
      if (!isApi(route)) return route.continue();
      matchCalls += 1;
      await new Promise((resolve) => setTimeout(resolve, 300));
      try {
        await route.fulfill({
          status: 201,
          contentType: 'application/json',
          body: JSON.stringify({ game_id: SOLO_GAME_ID })
        });
      } catch {
        // The page may abort the in-flight request when canceling; either way
        // the late response must not route the player into a game.
      }
    });

    await page.goto('/');
    await page.getByTestId('home-play-vs-ai-cta').click();
    await expect(page.getByTestId('match-queue-screen')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('match-cancel')).toBeVisible();
    await page.getByTestId('match-cancel').click();
    await expect(page).toHaveURL(/\/$/);
    await page.waitForTimeout(500);
    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByTestId('play-title')).toHaveCount(0);
    expect(matchCalls).toBe(1);
  });

  test('deferred match admission shows friendly bounded retry copy and remains cancelable', async ({
    page
  }) => {
    await routeQuietHome(page);

    let healthzCalled = false;
    let matchCalls = 0;

    await page.route('**/healthz/human-lane*', async (route) => {
      healthzCalled = true;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ waiting: 9, running: 3, max_running: 3 })
      });
    });

    await page.route('**/human/me', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ principal_id: 'guest-deferred', kind: 'guest', display_name: null })
      });
    });

    await page.route('**/human/consent', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          consented: true,
          required_versions: { TOS: '1', PRIVACY: '1', AGE_GATE: '1' }
        })
      });
    });

    await page.route('**/human/match', async (route) => {
      if (!isApi(route)) return route.continue();
      matchCalls += 1;
      await route.fulfill({
        status: 429,
        contentType: 'application/json',
        body: JSON.stringify({
          detail: 'human lane busy: waiting=9 running=3 max_running=3 cost_breaker=open'
        })
      });
    });

    await page.goto('/');
    await page.getByTestId('home-play-vs-ai-cta').click();
    await expect(page.getByTestId('match-queue-screen')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('match-state')).toContainText('Finding your table');
    await expect(page.getByTestId('match-status')).toContainText('Trying again');

    const visibleCopy = (await page.getByTestId('match-queue-screen').textContent()) ?? '';
    expect(visibleCopy).not.toMatch(/waiting=|running=|max_running|healthz|cost_breaker|worker/i);
    expect(healthzCalled).toBe(false);

    await page.getByTestId('match-cancel').click();
    await expect(page).toHaveURL(/\/$/);
    await page.waitForTimeout(900);
    expect(matchCalls).toBe(1);
  });

  test('returning consented guest skips re-consent and goes straight to match', async ({
    page
  }) => {
    await routeQuietHome(page);

    let guestCreateCalls = 0;
    let consentPostCalls = 0;
    let matchCalls = 0;
    await page.route('**/human/me', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ principal_id: 'guest-2', kind: 'guest', display_name: null })
      });
    });

    await page.route('**/human/guest', async (route) => {
      if (!isApi(route)) return route.continue();
      guestCreateCalls += 1;
      await route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({ principal_id: 'guest-2', kind: 'guest', display_name: null })
      });
    });

    await page.route('**/human/consent', async (route) => {
      if (!isApi(route)) return route.continue();
      if (route.request().method() === 'POST') consentPostCalls += 1;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          consented: true,
          required_versions: { TOS: '1', PRIVACY: '1', AGE_GATE: '1' }
        })
      });
    });

    await page.route('**/human/match', async (route) => {
      if (!isApi(route)) return route.continue();
      matchCalls += 1;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ game_id: SOLO_GAME_ID })
      });
    });

    await page.goto('/');
    await page.getByTestId('home-play-vs-ai-cta').click();
    await expect(page).toHaveURL(new RegExp(`/play/${SOLO_GAME_ID}$`), { timeout: 15_000 });
    await expect(page.getByTestId('home-consent-row')).toHaveCount(0);
    expect(guestCreateCalls).toBe(0);
    expect(consentPostCalls).toBe(0);
    expect(matchCalls).toBe(1);
  });

  test('renders public-surface KPIs and links the top-3 agents to the ladder', async ({ page }) => {
    await page.goto('/');

    await expect(page.getByTestId('home-kpis')).toBeVisible();
    await expect(page.getByTestId('home-kpi-live-now')).toBeVisible();
    await expect(page.getByTestId('home-kpi-recent-games')).toBeVisible();
    await expect(page.getByTestId('home-kpi-top-agent')).toBeVisible();

    // Live-now count comes from /public/live total — always a non-negative integer.
    await expect(page.getByTestId('home-kpi-live-now')).toHaveText(/^[0-9]+$/);

    // Recent count comes from /public/recent total_estimate.
    await expect(page.getByTestId('home-kpi-recent-games')).toHaveText(/^[0-9]+$/);

    // The smoke harness ingests at least one ranked game, so the ladder KPI fills in.
    const topAgent = page.getByTestId('home-kpi-top-agent');
    await expect(topAgent).not.toHaveText('—');

    const topRows = page.getByTestId('home-top-agent-row');
    await expect(topRows.first()).toBeVisible();
    const rowCount = await topRows.count();
    expect(rowCount).toBeGreaterThan(0);

    const firstLink = page.getByTestId('home-top-agent-link').first();
    await expect(firstLink).toHaveAttribute('href', '/ladder');
    await firstLink.click();
    await expect(page).toHaveURL(/\/ladder$/);
    await expect(page.getByTestId('ladder-table')).toBeVisible({ timeout: 15_000 });
  });

  test('matches the home visual snapshot', async ({ page }) => {
    test.skip(!process.env.PADRINO_E2E_VISUAL, 'visual regression opt-in only');
    await page.goto('/');
    await expect(page.getByTestId('home-kpis')).toBeVisible();
    await expect(page).toHaveScreenshot('home.png', {
      fullPage: true,
      mask: [page.getByTestId('home-kpi-live-now'), page.getByTestId('home-kpi-recent-games')]
    });
  });
});
