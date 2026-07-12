import { expect, test } from '@playwright/test';

test.describe('neo-noir design system', () => {
  test('offers keyboard-accessible tabs, tooltip, and dialog controls', async ({ page }) => {
    await page.goto('/laboratory');

    await expect(page.getByTestId('design-system-title')).toBeVisible();
    await expect(page.getByTestId('design-system-tabs').getByRole('tab')).toHaveCount(3);

    const signalsTab = page.getByRole('tab', { name: 'Signals' });
    await signalsTab.click();
    await expect(signalsTab).toHaveAttribute('aria-selected', 'true');
    await expect(page.getByTestId('design-system-statuses')).toBeVisible();

    await page.getByRole('tab', { name: 'Controls' }).click();
    const dialogTrigger = page.getByTestId('design-system-dialog-open');
    const tooltipTrigger = page.getByTestId('design-system-tooltip-trigger');
    await dialogTrigger.focus();
    await page.keyboard.press('Tab');
    await expect(tooltipTrigger).toBeFocused();
    await expect(tooltipTrigger).toHaveCSS('outline-style', 'solid');

    await dialogTrigger.click();
    await expect(page.getByRole('dialog')).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog')).toBeHidden();
  });

  test('pins the desktop component foundation', async ({ page }) => {
    test.skip(!process.env.PADRINO_E2E_VISUAL, 'visual regression opt-in only');
    await page.goto('/laboratory');
    await expect(page.getByTestId('design-system-title')).toBeVisible();
    await expect(page).toHaveScreenshot('design-system-desktop.png', { fullPage: true });
  });

  test('pins the mobile component foundation', async ({ page }) => {
    test.skip(!process.env.PADRINO_E2E_VISUAL, 'visual regression opt-in only');
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/laboratory');
    await expect(page.getByTestId('design-system-title')).toBeVisible();
    await expect(page).toHaveScreenshot('design-system-mobile.png', { fullPage: true });
  });
});
