import { expect, test } from '@playwright/test';

const HOW_TO_PLAY_DISMISSED_KEY = 'padrino:how-to-play-dismissed:v1';

type Page = import('@playwright/test').Page;
type Route = import('@playwright/test').Route;

function isApi(route: Route): boolean {
  const resource = route.request().resourceType();
  return resource === 'fetch' || resource === 'xhr';
}

async function routeQuietHome(page: Page): Promise<void> {
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

async function routeQuickPlayer(
  page: Page,
  options: { consented: boolean; walletMicros: number; connected: boolean }
): Promise<void> {
  await page.route('**/human/me', async (route) => {
    if (!isApi(route)) return route.continue();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ principal_id: 'quick-player', kind: 'account', display_name: null })
    });
  });
  await page.route('**/human/consent', async (route) => {
    if (!isApi(route)) return route.continue();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        consented: options.consented,
        required_versions: { TOS: '1', PRIVACY: '1', AGE_GATE: '1' }
      })
    });
  });
  await page.route('**/human/wallet', async (route) => {
    if (!isApi(route)) return route.continue();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        granted_micros: 5_000_000,
        available_micros: options.walletMicros,
        reserved_micros: 0,
        spent_micros: 5_000_000 - options.walletMicros,
        completion_subsidy_micros: 0,
        recent_entries: []
      })
    });
  });
  await page.route('**/human/openrouter/connect', async (route) => {
    if (!isApi(route)) return route.continue();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        connected: options.connected,
        label: options.connected ? 'Personal account' : null,
        display_suffix: options.connected ? '•••• 8721' : null,
        validated_at: options.connected ? '2026-07-12T12:00:00Z' : null,
        credit_capability: options.connected ? 'openrouter_user_key' : null
      })
    });
  });
}

function quote(overrides: Partial<Record<string, unknown>> = {}): Record<string, unknown> {
  return {
    funded: true,
    allowed: true,
    funding_source: 'PLATFORM',
    reason: 'ok',
    reasons: [],
    reservation_micros: 240_000,
    expected_total_micros: 240_000,
    funding_quote_hash: 'a'.repeat(64),
    estimate_hash: 'b'.repeat(64),
    model_hash: 'c'.repeat(64),
    catalog_hash: 'd'.repeat(64),
    rules_hash: 'e'.repeat(64),
    funding_hash: 'f'.repeat(64),
    byok_max_estimate_micros: null,
    ...overrides
  };
}

test.describe('game-mode home', () => {
  test('centers play around clear casual modes and a disabled ranked preview', async ({ page }) => {
    await routeQuietHome(page);
    await page.goto('/');

    await expect(page.getByTestId('game-mode-home')).toBeVisible();
    await expect(page.getByTestId('mode-card-quick-play')).toContainText(/casual/i);
    await expect(page.getByTestId('mode-card-quick-play')).toContainText(/20(?:–|-)?30\s+minutes/i);
    await expect(page.getByTestId('mode-card-custom-lobby')).toContainText(/always casual.*never ranked/i);
    await expect(page.getByTestId('mode-card-custom-lobby')).toContainText(/invite/i);
    await expect(page.getByTestId('mode-card-ranked')).toContainText(/coming next/i);
    await expect(page.getByTestId('mode-card-ranked-cta')).toBeDisabled();
  });

  test('gives first-time players a skippable two-minute briefing that can be reopened', async ({ page }) => {
    await routeQuietHome(page);
    await routeQuickPlayer(page, { consented: true, walletMicros: 5_000_000, connected: false });
    await page.goto('/');

    await page.getByTestId('home-play-vs-ai-cta').click();
    const briefing = page.getByTestId('how-to-play-modal');
    await expect(briefing).toBeVisible();
    await expect(briefing).toContainText(/day discussion/i);
    await expect(briefing).toContainText(/chat/i);
    await expect(briefing).toContainText(/vote/i);
    await expect(briefing).toContainText(/night actions/i);
    await expect(briefing).toContainText(/spot the ai/i);

    await page.getByTestId('how-to-play-continue').click();
    await expect(page.getByTestId('quick-play-funding')).toBeVisible();
    await expect
      .poll(() => page.evaluate((key) => window.localStorage.getItem(key), HOW_TO_PLAY_DISMISSED_KEY))
      .toBe('true');

    await page.getByRole('button', { name: 'Not now' }).click();
    await page.getByTestId('home-onboarding-reopen').click();
    await expect(briefing).toBeVisible();
    await page.getByTestId('how-to-play-back').click();
  });

  test('takes a guest through consent into a verified promotional-credit Quick Play quote', async ({
    page
  }) => {
    await page.addInitScript((key) => window.localStorage.setItem(key, 'true'), HOW_TO_PLAY_DISMISSED_KEY);
    await routeQuietHome(page);

    let guestCreated = false;
    let consented = false;
    await page.route('**/human/me', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: guestCreated ? 200 : 401,
        contentType: 'application/json',
        body: JSON.stringify(
          guestCreated
            ? { principal_id: 'guest-quick', kind: 'guest', display_name: null }
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
        body: JSON.stringify({ principal_id: 'guest-quick', kind: 'guest', display_name: null })
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
    await page.route('**/legal/documents/*', async (route) => {
      if (!isApi(route)) return route.continue();
      const kind = route.request().url().split('/').at(-1) ?? 'TOS';
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ kind, version_label: 'v1', content_hash: `${kind.toLowerCase()}-hash`, content: 'Terms' })
      });
    });
    await page.route('**/human/wallet', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          granted_micros: 5_000_000,
          available_micros: 3_750_000,
          reserved_micros: 0,
          spent_micros: 1_250_000,
          completion_subsidy_micros: 0,
          recent_entries: []
        })
      });
    });
    await page.route('**/human/openrouter/connect', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ connected: false, label: null, display_suffix: null, validated_at: null, credit_capability: null })
      });
    });
    await page.route('**/lobbies', async (route) => {
      if (!isApi(route)) return route.continue();
      expect(route.request().postDataJSON()).toMatchObject({
        built_in_ruleset_id: 'mini7_v1',
        human_seat_count: 1,
        autofill_policy: { funding_source: 'platform' }
      });
      await route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify({ id: 'quick-lobby' }) });
    });
    await page.route('**/lobbies/quick-lobby/funding-preflight', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(quote()) });
    });

    await page.goto('/');
    await page.getByTestId('home-play-vs-ai-cta').click();
    await expect(page.getByTestId('home-consent-card')).toBeVisible();
    await page.getByTestId('home-consent-accept').click();
    await expect(page.getByTestId('quick-play-funding')).toBeVisible();
    await expect(page.getByTestId('quick-funding-promo')).toContainText('$3.75');
    await expect(page.getByTestId('quick-funding-byok')).toContainText(/connect an account/i);
    await page.getByTestId('quick-get-quote').click();
    await expect(page.getByTestId('quick-bound-quote')).toContainText('$0.24');
    await expect(page.getByTestId('quick-bound-quote')).toContainText(/ready to launch/i);
  });

  test('binds the exact quote before creating the game', async ({ page }) => {
    await page.addInitScript((key) => window.localStorage.setItem(key, 'true'), HOW_TO_PLAY_DISMISSED_KEY);
    await routeQuietHome(page);
    await routeQuickPlayer(page, { consented: true, walletMicros: 1_000_000, connected: false });

    const steps: string[] = [];
    await page.route('**/lobbies', async (route) => {
      if (!isApi(route)) return route.continue();
      steps.push('lobby');
      await route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify({ id: 'funded-lobby' }) });
    });
    await page.route('**/lobbies/funded-lobby/funding-preflight', async (route) => {
      if (!isApi(route)) return route.continue();
      steps.push('quote');
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(quote()) });
    });
    await page.route('**/lobbies/funded-lobby/ready', async (route) => {
      if (!isApi(route)) return route.continue();
      steps.push('ready');
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({}) });
    });
    await page.route('**/lobbies/funded-lobby/lock', async (route) => {
      if (!isApi(route)) return route.continue();
      steps.push('lock');
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({}) });
    });
    await page.route('**/lobbies/funded-lobby/launch', async (route) => {
      if (!isApi(route)) return route.continue();
      steps.push('launch');
      expect(route.request().postDataJSON()).toEqual({ funding_quote_hash: 'a'.repeat(64), byok_max_estimate_micros: null });
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ game_id: 'bound-game' }) });
    });

    await page.goto('/');
    await page.getByTestId('home-play-vs-ai-cta').click();
    await page.getByTestId('quick-get-quote').click();
    await expect(page.getByTestId('quick-launch')).toBeVisible();
    expect(steps).toEqual(['lobby', 'quote']);
    await page.getByTestId('quick-launch').click();
    await expect(page).toHaveURL(/\/play\/bound-game$/);
    expect(steps).toEqual(['lobby', 'quote', 'ready', 'lock', 'launch']);
  });

  test('supports connected OpenRouter funding on mobile and handles exhausted promo inline', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.addInitScript((key) => window.localStorage.setItem(key, 'true'), HOW_TO_PLAY_DISMISSED_KEY);
    await routeQuietHome(page);
    await routeQuickPlayer(page, { consented: true, walletMicros: 0, connected: true });
    await page.route('**/lobbies', async (route) => {
      if (!isApi(route)) return route.continue();
      expect(route.request().postDataJSON().autofill_policy.funding_source).toBe('byok');
      await route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify({ id: 'byok-lobby' }) });
    });
    await page.route('**/lobbies/byok-lobby/funding-preflight', async (route) => {
      if (!isApi(route)) return route.continue();
      expect(route.request().postDataJSON()).toEqual({ byok_max_estimate_micros: 5_000_000 });
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(quote({
          funding_source: 'BYOK_OWNER',
          reservation_micros: null,
          expected_total_micros: 430_000,
          byok_max_estimate_micros: 5_000_000
        }))
      });
    });

    await page.goto('/');
    await page.getByTestId('home-play-vs-ai-cta').click();
    await expect(page.getByTestId('quick-play-funding')).toBeVisible();
    await expect(page.getByTestId('quick-promo-exhausted')).toContainText(/no usable promotional credit/i);
    await expect(page.getByLabel('Use promotional credit')).toBeDisabled();
    await expect(page.getByLabel('Use connected OpenRouter')).toBeChecked();
    await page.getByTestId('quick-byok-maximum').fill('5.00');
    await page.getByTestId('quick-get-quote').click();
    await expect(page.getByTestId('quick-bound-quote')).toContainText('$0.43');
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  });

  test('keeps public loading failures branded and actionable', async ({ page }) => {
    await page.route('**/public/live*', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.abort('failed');
    });
    await page.route('**/public/recent*', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.abort('failed');
    });
    await page.route('**/public/rulesets', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.abort('failed');
    });

    await page.goto('/');
    const offline = page.getByTestId('home-public-offline');
    await expect(offline).toBeVisible();
    await expect(offline).toContainText(/observatory signal is unavailable/i);
    await expect(offline).toContainText(/reconnect/i);
    expect((await offline.textContent()) ?? '').not.toMatch(/fetch|networkerror|http|undefined/i);
  });

  test('renders public-surface KPIs and links the top agents to the ladder', async ({ page }) => {
    await page.route('**/public/live*', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], total: 2 }) });
    });
    await page.route('**/public/recent*', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], next_cursor: null, total_estimate: 9 }) });
    });
    await page.route('**/public/rulesets', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [{ ruleset_id: 'mini7_v1', label: 'Mini 7 canonical team', player_count: 7, rating_context_kind: 'CANONICAL_TEAM', is_canonical: true }]
        })
      });
    });
    await page.route('**/public/ladder*', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ruleset_id: 'mini7_v1',
          entries: [{ agent_build_id: 'agent-one', display_name: 'Observatory One', version: 'v1', ordinal: 31, provisional: false, games: 20, last_game_at: null }],
          next_cursor: null,
          total_estimate: 1
        })
      });
    });
    await page.goto('/');

    await expect(page.getByTestId('home-kpis')).toBeVisible();
    await expect(page.getByTestId('home-kpi-live-now')).toHaveText(/^[0-9]+$/);
    await expect(page.getByTestId('home-kpi-recent-games')).toHaveText(/^[0-9]+$/);
    await expect(page.getByTestId('home-kpi-top-agent')).not.toHaveText('Calibrating');
    const firstLink = page.getByTestId('home-top-agent-link').first();
    await expect(firstLink).toHaveAttribute('href', '/ladder');
    await firstLink.click();
    await expect(page).toHaveURL(/\/ladder$/);
    await expect(page.getByTestId('ladder-table')).toBeVisible({ timeout: 15_000 });
  });
});
