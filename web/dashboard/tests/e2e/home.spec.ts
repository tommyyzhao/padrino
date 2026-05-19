import { expect, test } from '@playwright/test';

test.describe('home', () => {
  test('renders the three KPIs and links the top-3 models to the leaderboard', async ({ page }) => {
    await page.goto('/');

    await expect(page.getByTestId('home-kpis')).toBeVisible();
    await expect(page.getByTestId('home-kpi-total-games')).toBeVisible();
    await expect(page.getByTestId('home-kpi-active-gauntlets')).toBeVisible();
    await expect(page.getByTestId('home-kpi-top-model')).toBeVisible();

    const totalGamesText = (await page.getByTestId('home-kpi-total-games').textContent()) ?? '';
    expect(totalGamesText.trim()).toMatch(/^[0-9]+$/);

    const topModel = page.getByTestId('home-kpi-top-model');
    await expect(topModel).not.toHaveText('—');

    const topRows = page.getByTestId('home-top-model-row');
    await expect(topRows.first()).toBeVisible();
    const rowCount = await topRows.count();
    expect(rowCount).toBeGreaterThan(0);

    const firstLink = page.getByTestId('home-top-model-link').first();
    await expect(firstLink).toHaveAttribute('href', '/leaderboard');
    await firstLink.click();
    await expect(page).toHaveURL(/\/leaderboard$/);
    await expect(page.getByTestId('leaderboard-title')).toBeVisible();
  });

  test('matches the home visual snapshot', async ({ page }) => {
    test.skip(!process.env.PADRINO_E2E_VISUAL, 'visual regression opt-in only');
    await page.goto('/');
    await expect(page.getByTestId('home-kpis')).toBeVisible();
    await expect(page).toHaveScreenshot('home.png', {
      fullPage: true,
      mask: [page.getByTestId('home-kpi-total-games'), page.getByTestId('home-kpi-active-gauntlets')]
    });
  });
});
