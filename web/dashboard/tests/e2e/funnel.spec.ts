import { expect, test } from '@playwright/test';

const HOW_TO_PLAY_DISMISSED_KEY = 'padrino:how-to-play-dismissed:v1';

test.describe('US-287 cold visitor funnel', () => {
  test('cold visitors consent before Quick Play exposes funding choices', async ({ page }) => {
    await page.addInitScript((key) => window.localStorage.removeItem(key), HOW_TO_PLAY_DISMISSED_KEY);

    await page.goto('/');
    await expect(page.getByTestId('home-play-vs-ai-cta')).toBeVisible();
    await page.getByTestId('home-play-vs-ai-cta').click();
    await expect(page.getByTestId('how-to-play-modal')).toBeVisible();
    await page.getByTestId('how-to-play-continue').click();
    await expect(page.getByTestId('home-consent-row')).toBeVisible({ timeout: 15_000 });
    await page.getByTestId('home-consent-accept').click();

    const funding = page.getByTestId('quick-play-funding');
    await expect(funding).toBeVisible({ timeout: 15_000 });
    await expect(funding).toContainText(/promotional credit|connected openrouter/i);
    await expect(funding).toContainText(/casual and anonymous/i);
    await expect(page).toHaveURL(/\/$/);
  });
});
