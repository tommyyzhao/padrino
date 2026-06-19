import { expect, test } from '@playwright/test';

test.describe('games', () => {
  // US-111: the games browser is a public-surface-only page. It is sourced
  // exclusively from `/public/live` (LIVE games -> /watch/{id}) and
  // `/public/recent` (finished games -> /games/{id}); it never calls a private
  // endpoint. When the public broadcast surface has no live or recent games
  // (the spectator-only deployment, and the e2e smoke harness, which ingests
  // games without populating the LIVE/RECENT broadcast indexes) the page must
  // degrade cleanly: no infinite spinner, no hard error, just an empty state.
  test('renders the public games browser and degrades cleanly', async ({ page }) => {
    await page.goto('/games');

    await expect(page.getByTestId('games-title')).toBeVisible();
    // The page resolves load() -> the spinner clears regardless of data.
    await expect(page.getByTestId('games-loading')).toHaveCount(0, { timeout: 30_000 });
    // No hard error on the public surface.
    await expect(page.getByTestId('games-error')).toHaveCount(0);

    // Filter to completed (finished games from /public/recent).
    await page.getByTestId('games-filter-completed').click();
    await expect(page.getByTestId('games-loading')).toHaveCount(0, { timeout: 15_000 });

    const table = page.getByTestId('games-table');
    const empty = page.getByTestId('games-empty');
    const rows = page.getByTestId('games-row');

    if (await table.count()) {
      // Data path: the completed table is rendered and every finished game
      // links to its replay at /games/{id}.
      await expect(table).toHaveAttribute('data-status', 'COMPLETED');
      await expect(rows.first()).toBeVisible();
      await expect(rows.first().getByTestId('games-row-status')).toHaveText('COMPLETED');

      const href = await rows.first().getByTestId('games-open-link').getAttribute('href');
      expect(href).toMatch(/^\/games\/[0-9a-fA-F-]+$/);

      // Open the first completed game's replay and scrub through phases.
      await rows.first().getByTestId('games-open-link').click();
      await expect(page).toHaveURL(/\/games\/[0-9a-fA-F-]+$/);
      await expect(page.getByTestId('replay-title')).toBeVisible();
      await expect(page.getByTestId('replay-loading')).toHaveCount(0, { timeout: 30_000 });

      const phasePills = page.getByTestId('replay-phase-pill');
      await expect(phasePills.first()).toBeVisible();
      const phaseCount = await phasePills.count();
      expect(phaseCount).toBeGreaterThan(0);

      const nextBtn = page.getByTestId('replay-next');
      const prevBtn = page.getByTestId('replay-prev');
      await expect(prevBtn).toBeDisabled();
      let steps = 0;
      while (!(await nextBtn.isDisabled()) && steps < phaseCount + 2) {
        await nextBtn.click();
        steps += 1;
      }
      expect(steps).toBeGreaterThan(0);
      await expect(nextBtn).toBeDisabled();
      await expect(prevBtn).toBeEnabled();

      await phasePills.first().click();
      await expect(phasePills.first()).toHaveAttribute('data-active', 'true');
      await expect(prevBtn).toBeDisabled();
    } else {
      // Degraded path: no recent broadcast games -> clean empty state, never an
      // infinite spinner and never a hard error.
      await expect(empty).toBeVisible();
    }
  });

  test('matches the games-list visual snapshot', async ({ page }) => {
    test.skip(!process.env.PADRINO_E2E_VISUAL, 'visual regression opt-in only');
    await page.goto('/games');
    await expect(page.getByTestId('games-loading')).toHaveCount(0, { timeout: 30_000 });
    await expect(page).toHaveScreenshot('games.png', {
      fullPage: true,
      mask: [page.getByTestId('games-table')]
    });
  });
});
