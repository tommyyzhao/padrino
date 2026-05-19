import { expect, test } from '@playwright/test';

test.describe('games', () => {
  test('opens the first completed game and scrubs through phases', async ({ page }) => {
    await page.goto('/games');

    await expect(page.getByTestId('games-title')).toBeVisible();
    await expect(page.getByTestId('games-loading')).toHaveCount(0, { timeout: 30_000 });

    // The smoke harness ingests at least one COMPLETED game. Narrow with the
    // filter so the first row is guaranteed to be terminal.
    await page.getByTestId('games-filter-completed').click();
    await expect(page.getByTestId('games-loading')).toHaveCount(0, { timeout: 15_000 });
    await expect(page.getByTestId('games-table')).toHaveAttribute('data-status', 'COMPLETED');

    const rows = page.getByTestId('games-row');
    await expect(rows.first()).toBeVisible();
    expect(await rows.count()).toBeGreaterThan(0);
    await expect(rows.first().getByTestId('games-row-status')).toHaveText('COMPLETED');

    // Open the first completed game's replay.
    await rows.first().getByTestId('games-open-link').click();
    await expect(page).toHaveURL(/\/games\/[0-9a-fA-F-]+$/);
    await expect(page.getByTestId('replay-title')).toBeVisible();
    await expect(page.getByTestId('replay-loading')).toHaveCount(0, { timeout: 30_000 });

    const phasePills = page.getByTestId('replay-phase-pill');
    await expect(phasePills.first()).toBeVisible();
    const phaseCount = await phasePills.count();
    expect(phaseCount).toBeGreaterThan(0);

    // Scrub forward through every phase using the next button.
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

    // Jump straight back to the first phase by clicking the pill.
    await phasePills.first().click();
    await expect(phasePills.first()).toHaveAttribute('data-active', 'true');
    await expect(prevBtn).toBeDisabled();
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
