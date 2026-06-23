import { expect, test } from '@playwright/test';
import { expectIdentityBlind } from './helpers/identityBlind';

// US-092: Live/recent lobby page.
//
// Both /public/live and /public/recent are intercepted via page.route so these
// tests run without a live backend with broadcast-state data.  Two scenarios:
//   1. Live card renders phase / players_alive / watch link and NO outcome.
//   2. Recent card renders winner and NO live-state badge.

const LIVE_GAME_ID = 'aaaa0001-0001-0001-0001-aaaaaaaaaaaa';
const RECENT_GAME_ID = 'bbbb0002-0002-0002-0002-bbbbbbbbbbbb';

test.describe('lobby', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('**/public/live*', async (route) => {
      if (route.request().resourceType() === 'fetch' || route.request().resourceType() === 'xhr') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            items: [
              {
                game_id: LIVE_GAME_ID,
                ruleset_id: 'mini7_v1',
                current_phase: 'Day 1',
                players_alive: 5
              }
            ],
            total: 1
          })
        });
      } else {
        await route.continue();
      }
    });

    await page.route('**/public/recent*', async (route) => {
      if (route.request().resourceType() === 'fetch' || route.request().resourceType() === 'xhr') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
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
          })
        });
      } else {
        await route.continue();
      }
    });
  });

  test('live card renders phase and players alive; no outcome shown (spoiler safe)', async ({
    page
  }) => {
    await page.goto('/');

    // Live section is present
    await expect(page.getByTestId('lobby-live-section')).toBeVisible();

    // Wait for the card to appear (data loads asynchronously)
    const liveCard = page.getByTestId('lobby-live-card').first();
    await expect(liveCard).toBeVisible({ timeout: 15_000 });

    // Phase and players alive rendered on the live card
    await expect(page.getByTestId('lobby-live-phase').first()).toHaveText('Day 1');
    await expect(page.getByTestId('lobby-live-players').first()).toContainText('5');

    // Spoiler safety: no winner shown inside the live section (recent section may have one)
    const liveSection = page.getByTestId('lobby-live-section');
    await expect(liveSection.getByTestId('lobby-recent-winner')).toHaveCount(0);

    // Card links to the watch page for this game
    await expect(liveCard).toHaveAttribute('href', `/watch/${LIVE_GAME_ID}`);
  });

  test('recent card renders winner and links to watch page', async ({ page }) => {
    await page.goto('/');

    // Recent section is present
    await expect(page.getByTestId('lobby-recent-section')).toBeVisible();

    // Wait for the card to appear
    const recentCard = page.getByTestId('lobby-recent-card').first();
    await expect(recentCard).toBeVisible({ timeout: 15_000 });

    // Winner is exposed on the recent card
    await expect(page.getByTestId('lobby-recent-winner').first()).toContainText('TOWN');

    // Card links to the watch page for this game
    await expect(recentCard).toHaveAttribute('href', `/watch/${RECENT_GAME_ID}`);
  });
});

// US-154: Frontend lobby UI — guest-quickplay join from an invite link, the
// one-tap consent + 16+ gate, count-only roster, and ready-up.
//
// The smoke harness has no human-multiplayer lobby data, so every /human/* and
// /lobbies/* call is intercepted via `page.route` with deterministic fixtures
// (the same pattern leaderboard.spec.ts uses). Anonymity (AGENTS.md rule 7) is
// asserted by checking the roster surfaces ONLY counts, never a per-seat
// human/AI map.
const LOBBY_ID = 'cccc0003-0003-0003-0003-cccccccccccc';
const INVITE_TOKEN = 'invite-abc123';
const HOST_PRINCIPAL = 'pppp1111-1111-1111-1111-pppppppppppp';
const GUEST_PRINCIPAL = 'gggg2222-2222-2222-2222-gggggggggggg';
const RULESET_OPTIONS = [
  {
    ruleset_id: 'mini7_v1',
    label: 'Mini 7 canonical team',
    player_count: 7,
    rating_context_kind: 'CANONICAL_TEAM',
    is_canonical: true
  },
  {
    ruleset_id: 'roleblock10_v1',
    label: 'Roleblock 10 canonical team',
    player_count: 10,
    rating_context_kind: 'CANONICAL_TEAM',
    is_canonical: true
  }
];

function lobbySummary(status = 'OPEN') {
  return {
    id: LOBBY_ID,
    ruleset_id: 'mini7_v1',
    identity_mode: 'ANONYMOUS',
    theme_pack_id: null,
    stakes: 'CASUAL',
    ranked: false,
    integrity_acknowledged: false,
    status,
    invite_token: INVITE_TOKEN,
    host_principal_id: HOST_PRINCIPAL,
    league_id: 'llll0000-0000-0000-0000-llllllllllll',
    game_id: null,
    member_count: 2,
    composition: { human_count: 2, ai_count: 5, total: 7 }
  };
}

function lobbyRoster(guestReady: boolean) {
  return {
    id: LOBBY_ID,
    status: 'OPEN',
    member_count: 2,
    composition: { human_count: 2, ai_count: 5, total: 7 },
    members: [
      { member_id: HOST_PRINCIPAL, is_host: true, ready: true, present: true },
      { member_id: GUEST_PRINCIPAL, is_host: false, ready: guestReady, present: true }
    ]
  };
}

test.describe('lobby UI (US-154)', () => {
  async function routeRulesets(page: import('@playwright/test').Page) {
    await page.route('**/public/rulesets', async (route) => {
      const t = route.request().resourceType();
      if (t !== 'fetch' && t !== 'xhr') return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: RULESET_OPTIONS })
      });
    });
  }

  test('guest joins from an invite link, accepts consent, and readies up', async ({ page }) => {
    let consented = false;
    let guestReady = false;

    const isApi = (route: import('@playwright/test').Route) => {
      const t = route.request().resourceType();
      return t === 'fetch' || t === 'xhr';
    };

    await routeRulesets(page);

    // Guest quickplay: minting a guest principal (no signup).
    await page.route('**/human/guest', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ principal_id: GUEST_PRINCIPAL, kind: 'guest', display_name: null })
      });
    });

    await page.route('**/human/me', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          principal_id: GUEST_PRINCIPAL,
          kind: 'guest',
          display_name: 'Friend'
        })
      });
    });

    // Consent status flips to true once the one-tap consent is POSTed.
    await page.route('**/human/consent', async (route) => {
      if (!isApi(route)) return route.continue();
      if (route.request().method() === 'POST') consented = true;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ consented, required_versions: { TOS: '1', PRIVACY: '1', AGE_GATE: '1' } })
      });
    });

    await page.route(`**/lobbies/join/${INVITE_TOKEN}`, async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(lobbySummary())
      });
    });

    await page.route(`**/lobbies/${LOBBY_ID}/roster`, async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(lobbyRoster(guestReady))
      });
    });

    await page.route(`**/lobbies/${LOBBY_ID}/ready`, async (route) => {
      if (!isApi(route)) return route.continue();
      guestReady = true;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(lobbyRoster(true))
      });
    });

    await page.route(`**/lobbies/${LOBBY_ID}/heartbeat`, async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(lobbyRoster(guestReady))
      });
    });

    await page.route(`**/lobbies/${LOBBY_ID}`, async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(lobbySummary())
      });
    });

    // Land on the invite link: /lobby?invite=<token> prefills the join token.
    await page.goto(`/lobby?invite=${INVITE_TOKEN}`);
    await expect(page.getByTestId('lobby-join-token')).toHaveValue(INVITE_TOKEN);

    // Join as a guest — no signup.
    await page.getByTestId('lobby-join-submit').click();
    await expect(page).toHaveURL(new RegExp(`/lobby/${LOBBY_ID}$`), { timeout: 15_000 });

    // The consent + 16+ gate is shown before play; an optional sign-in CTA too.
    await expect(page.getByTestId('lobby-consent-row')).toBeVisible();
    await expect(page.getByTestId('lobby-signin-cta')).toBeVisible();

    // Composition is counts-only — never a per-seat human/AI map (anonymity).
    await expect(page.getByTestId('lobby-composition')).toContainText('2 humans');
    await expect(page.getByTestId('lobby-composition')).toContainText('5 AI');

    // One-tap consent.
    await page.getByTestId('lobby-consent-accept').click();

    // After consent, the ready control appears; ready up.
    const readyToggle = page.getByTestId('lobby-ready-toggle');
    await expect(readyToggle).toBeVisible({ timeout: 15_000 });
    await readyToggle.click();
    await expect(page.getByTestId('lobby-ready-state')).toHaveText('Ready');

    // Anonymity: the rendered roster exposes no human/AI seat markers.
    await expectIdentityBlind(page.getByTestId('lobby-roster'));
  });

  test('create form shows ruleset, identity mode, ranked toggle, and CASUAL default', async ({
    page
  }) => {
    await routeRulesets(page);
    await page.goto('/lobby');
    await expect(page.getByTestId('lobby-create-form')).toBeVisible();
    const rulesetSelect = page.getByTestId('lobby-create-ruleset');
    await expect(rulesetSelect).toBeVisible();
    await expect(rulesetSelect.locator('option')).toHaveCount(2);
    await expect(rulesetSelect.locator('option[value="roleblock10_v1"]')).toHaveText(
      'Roleblock 10 canonical team (10 players)'
    );
    await rulesetSelect.selectOption('roleblock10_v1');
    await expect(rulesetSelect).toHaveValue('roleblock10_v1');
    await expect(page.getByTestId('lobby-create-identity-mode')).toBeVisible();
    await expect(page.getByTestId('lobby-create-ranked')).not.toBeChecked();
    await expect(page.getByTestId('lobby-create-stakes')).toContainText('CASUAL');
  });

  test('ranked create toggle submits ranked lobbies without identity markers', async ({ page }) => {
    let createBody: Record<string, unknown> | null = null;
    const isApi = (route: import('@playwright/test').Route) => {
      const t = route.request().resourceType();
      return t === 'fetch' || t === 'xhr';
    };

    await routeRulesets(page);
    await page.route('**/human/guest', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({ principal_id: HOST_PRINCIPAL, kind: 'guest', display_name: null })
      });
    });
    await page.route('**/lobbies', async (route) => {
      if (!isApi(route)) return route.continue();
      createBody = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({ ...lobbySummary(), ranked: true })
      });
    });

    await page.goto('/lobby');
    await page.getByTestId('lobby-create-ranked').check();
    await page.getByTestId('lobby-create-integrity-ack').check();
    await expect(page.getByTestId('lobby-create-stakes')).toContainText('RANKED');
    await page.getByTestId('lobby-create-submit').click();

    await expect.poll(() => createBody).toMatchObject({
      ruleset_id: 'mini7_v1',
      identity_mode: 'ANONYMOUS',
      ranked: true,
      integrity_acknowledged: true
    });
    expect(JSON.stringify(createBody)).not.toMatch(/seat_kind|is_human/i);
  });
});
