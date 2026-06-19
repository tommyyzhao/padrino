import { expect, test } from '@playwright/test';

test.describe('home', () => {
  test('renders public-surface KPIs and links the top-3 agents to the ladder', async ({ page }) => {
    await page.goto('/');

    await expect(page.getByTestId('home-kpis')).toBeVisible();
    await expect(page.getByTestId('home-kpi-live-now')).toBeVisible();
    await expect(page.getByTestId('home-kpi-recent-games')).toBeVisible();
    await expect(page.getByTestId('home-kpi-top-agent')).toBeVisible();

    // Live-now count comes from /public/live total — always a non-negative integer.
    const liveNowText = (await page.getByTestId('home-kpi-live-now').textContent()) ?? '';
    expect(liveNowText.trim()).toMatch(/^[0-9]+$/);

    // Recent count comes from /public/recent total_estimate.
    const recentText = (await page.getByTestId('home-kpi-recent-games').textContent()) ?? '';
    expect(recentText.trim()).toMatch(/^[0-9]+$/);

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
