import { expect, test } from '@playwright/test';

test.describe('leaderboard', () => {
  test('loads at least one row, switches Town/Mafia tabs, and paginates', async ({ page }) => {
    await page.goto('/leaderboard');

    await expect(page.getByTestId('leaderboard-title')).toBeVisible();
    await expect(page.getByTestId('leaderboard-loading')).toHaveCount(0, { timeout: 30_000 });

    const rows = page.getByTestId('leaderboard-row');
    await expect(rows.first()).toBeVisible();
    expect(await rows.count()).toBeGreaterThan(0);

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

    // Pagination controls are present; clicking next when disabled is a no-op,
    // so we just assert the buttons exist and have a sane disabled state.
    const nextBtn = page.getByTestId('leaderboard-next');
    const prevBtn = page.getByTestId('leaderboard-prev');
    await expect(nextBtn).toBeVisible();
    await expect(prevBtn).toBeVisible();
    // Previous should always start disabled (no history yet).
    await expect(prevBtn).toBeDisabled();

    const nextDisabled = await nextBtn.isDisabled();
    if (!nextDisabled) {
      await nextBtn.click();
      await expect(page.getByTestId('leaderboard-loading')).toHaveCount(0, { timeout: 15_000 });
      await expect(prevBtn).toBeEnabled();
      await prevBtn.click();
      await expect(page.getByTestId('leaderboard-loading')).toHaveCount(0, { timeout: 15_000 });
    }
  });

  test('matches the leaderboard visual snapshot', async ({ page }) => {
    test.skip(!process.env.PADRINO_E2E_VISUAL, 'visual regression opt-in only');
    await page.goto('/leaderboard');
    await expect(page.getByTestId('leaderboard-loading')).toHaveCount(0, { timeout: 30_000 });
    await expect(page).toHaveScreenshot('leaderboard.png', {
      fullPage: true,
      mask: [page.getByTestId('leaderboard-table')]
    });
  });
});
