import { expect, test } from '@playwright/test';

// US-111 / US-118: the games browser is a public-surface-only page sourced
// exclusively from `/public/live` (LIVE games -> /watch/{id}) and
// `/public/recent` (finished games -> /games/{id}); it never calls a private
// endpoint.
//
// US-118 (flake burn-down): the spec is now HERMETIC. Rather than depend on
// whatever the smoke harness happens to seed into the broadcast indexes (which
// is machine/state-dependent — the smoke flow ingests a game but does not
// reliably promote one to RECENT/LIVE), the `/public/*` responses are
// intercepted via `page.route` with deterministic seeded data so both the
// data path (populated table + replay scrub) and the degraded empty state are
// exercised on purpose, never by luck.

const RECENT_GAME_ID = 'cccc0003-0003-0003-0003-cccccccccccc';

const RECENT_INDEX = {
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
};

const LIVE_INDEX = { items: [], total: 0 };

// A two-phase replay ending in a terminal frame: enough to scrub forward
// through phases and assert the next/prev disabled states deterministically.
const EVENTS = {
  game_id: RECENT_GAME_ID,
  items: [
    {
      sequence: 1,
      event_type: 'GameStarted',
      phase: 'Day 1',
      visibility: 'PUBLIC',
      actor_player_id: null,
      payload: { ruleset_id: 'mini7_v1' },
      prev_event_hash: '0'.repeat(64),
      event_hash: 'a'.repeat(64)
    },
    {
      sequence: 2,
      event_type: 'PublicMessageSubmitted',
      phase: 'Day 1',
      visibility: 'PUBLIC',
      actor_player_id: 'pppppppp-0001-0001-0001-pppppppppppp',
      payload: { text: 'hello town' },
      prev_event_hash: 'a'.repeat(64),
      event_hash: 'b'.repeat(64)
    },
    {
      sequence: 3,
      event_type: 'GameTerminated',
      phase: 'Day 2',
      visibility: 'PUBLIC',
      actor_player_id: null,
      payload: { winner: 'TOWN', reason: 'all_mafia_eliminated' },
      prev_event_hash: 'b'.repeat(64),
      event_hash: 'c'.repeat(64)
    }
  ],
  next_cursor: null,
  total_estimate: 3
};

function mockJson(
  page: import('@playwright/test').Page,
  glob: string,
  body: unknown,
  status = 200
) {
  return page.route(glob, async (route) => {
    if (route.request().resourceType() === 'fetch' || route.request().resourceType() === 'xhr') {
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

test.describe('games', () => {
  test('renders the public games browser and scrubs a completed replay', async ({ page }) => {
    await mockJson(page, '**/public/live*', LIVE_INDEX);
    await mockJson(page, '**/public/recent*', RECENT_INDEX);
    await mockJson(page, `**/public/games/${RECENT_GAME_ID}/events*`, EVENTS);

    await page.goto('/games');

    await expect(page.getByTestId('games-title')).toBeVisible();
    await expect(page.getByTestId('games-loading')).toHaveCount(0, { timeout: 30_000 });
    await expect(page.getByTestId('games-error')).toHaveCount(0);

    // Filter to completed (finished games from /public/recent).
    await page.getByTestId('games-filter-completed').click();
    await expect(page.getByTestId('games-loading')).toHaveCount(0, { timeout: 15_000 });

    const table = page.getByTestId('games-table');
    const rows = page.getByTestId('games-row');

    await expect(table).toHaveAttribute('data-status', 'COMPLETED');
    await expect(rows.first()).toBeVisible();
    await expect(rows.first().getByTestId('games-row-status')).toHaveText('COMPLETED');

    const href = await rows.first().getByTestId('games-open-link').getAttribute('href');
    expect(href).toBe(`/games/${RECENT_GAME_ID}`);

    // Open the completed game's replay and scrub through phases.
    await rows.first().getByTestId('games-open-link').click();
    await expect(page).toHaveURL(`/games/${RECENT_GAME_ID}`);
    await expect(page.getByTestId('replay-title')).toBeVisible();
    await expect(page.getByTestId('replay-loading')).toHaveCount(0, { timeout: 30_000 });

    const phasePills = page.getByTestId('replay-phase-pill');
    await expect(phasePills.first()).toBeVisible();
    const phaseCount = await phasePills.count();
    expect(phaseCount).toBe(2);

    const nextBtn = page.getByTestId('replay-next');
    const prevBtn = page.getByTestId('replay-prev');
    await expect(prevBtn).toBeDisabled();
    let steps = 0;
    while (!(await nextBtn.isDisabled()) && steps < phaseCount + 2) {
      await nextBtn.click();
      steps += 1;
    }
    expect(steps).toBe(1);
    await expect(nextBtn).toBeDisabled();
    await expect(prevBtn).toBeEnabled();

    await phasePills.first().click();
    await expect(phasePills.first()).toHaveAttribute('data-active', 'true');
    await expect(prevBtn).toBeDisabled();
  });

  test('degrades cleanly when the public surface has no games', async ({ page }) => {
    await mockJson(page, '**/public/live*', { items: [], total: 0 });
    await mockJson(page, '**/public/recent*', { items: [], next_cursor: null, total_estimate: 0 });

    await page.goto('/games');

    await expect(page.getByTestId('games-title')).toBeVisible();
    await expect(page.getByTestId('games-loading')).toHaveCount(0, { timeout: 30_000 });
    await expect(page.getByTestId('games-error')).toHaveCount(0);
    await expect(page.getByTestId('games-empty')).toBeVisible();
  });

  test('matches the games-list visual snapshot', async ({ page }) => {
    test.skip(!process.env.PADRINO_E2E_VISUAL, 'visual regression opt-in only');
    await mockJson(page, '**/public/live*', LIVE_INDEX);
    await mockJson(page, '**/public/recent*', RECENT_INDEX);
    await page.goto('/games');
    await expect(page.getByTestId('games-loading')).toHaveCount(0, { timeout: 30_000 });
    await expect(page).toHaveScreenshot('games.png', {
      fullPage: true,
      mask: [page.getByTestId('games-table')]
    });
  });
});
