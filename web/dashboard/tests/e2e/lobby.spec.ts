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
      {
        member_id: 'member-host-safe',
        display_label: 'Host Friend',
        is_self: false,
        host: true,
        ready: true,
        present: true
      },
      {
        member_id: 'member-guest-safe',
        display_label: 'Friend',
        is_self: true,
        host: false,
        ready: guestReady,
        present: true
      }
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
    await expect(page.getByTestId('lobby-roster')).toContainText('Friend');
    await expectIdentityBlind(page.getByTestId('lobby-roster'));
  });

  test('keeps the host waiting room live, casual, and safely launch-gated', async ({ page }) => {
    const isApi = (route: import('@playwright/test').Route) => {
      const type = route.request().resourceType();
      return type === 'fetch' || type === 'xhr';
    };
    let streamConnections = 0;
    let transferred = false;

    const hostSummary = () => ({
      ...lobbySummary(),
      member_count: 2,
      ai_roster: [
        { canonical_slug: 'openai/fast-oracle', count: 2 },
        { canonical_slug: 'acme/quiet-witness', count: 3 }
      ]
    });
    const hostRoster = () => ({
      id: LOBBY_ID,
      status: 'OPEN',
      member_count: 2,
      composition: { human_count: 2, ai_count: 5, total: 7 },
      launch_readiness: {
        allowed: false,
        reasons: ['not_ready_member']
      },
      members: [
        {
          member_id: 'host-member-safe',
          display_label: 'Host Friend',
          is_self: true,
          host: !transferred,
          ready: true,
          present: true
        },
        {
          member_id: 'friend-member-safe',
          display_label: 'Friend',
          is_self: false,
          host: transferred,
          ready: false,
          present: true
        }
      ]
    });

    await page.route('**/human/me', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ principal_id: HOST_PRINCIPAL, kind: 'guest', display_name: 'Host Friend' })
      });
    });
    await page.route('**/human/consent', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ consented: true, required_versions: {} })
      });
    });
    await page.route(`**/lobbies/${LOBBY_ID}/funding-preflight`, async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          funded: true,
          allowed: true,
          funding_source: 'PLATFORM',
          reason: 'ok',
          reasons: [],
          reservation_micros: 120000,
          expected_total_micros: 120000,
          funding_quote_hash: 'q'.repeat(64),
          estimate_hash: 'e'.repeat(64),
          model_hash: 'm'.repeat(64),
          catalog_hash: 'c'.repeat(64),
          rules_hash: 'r'.repeat(64),
          funding_hash: 'f'.repeat(64),
          byok_max_estimate_micros: null
        })
      });
    });
    await page.route(`**/lobbies/${LOBBY_ID}/transfer/friend-member-safe`, async (route) => {
      if (!isApi(route)) return route.continue();
      transferred = true;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(hostRoster()) });
    });
    await page.route(`**/lobbies/${LOBBY_ID}/leave`, async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(hostSummary()) });
    });
    await page.route(`**/lobbies/${LOBBY_ID}/heartbeat`, async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(hostRoster()) });
    });
    await page.route(`**/lobbies/${LOBBY_ID}/roster`, async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(hostRoster()) });
    });
    await page.route(`**/lobbies/${LOBBY_ID}`, async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(hostSummary()) });
    });
    // Register after the broad lobby route: Playwright evaluates the most
    // recently registered matching route first, so the EventSource handshake
    // must win over the summary fixture.
    await page.route(`**/lobbies/${LOBBY_ID}/stream`, async (route) => {
      streamConnections += 1;
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: `data: ${JSON.stringify({ type: 'lobby_state', ...hostRoster() })}\n\n`
      });
    });

    await page.goto(`/lobby/${LOBBY_ID}?funding_quote_hash=${'q'.repeat(64)}&funding_source=platform`);

    await expect.poll(() => streamConnections).toBeGreaterThan(0);
    await expect(page.getByTestId('lobby-casual-notice')).toContainText(/casual only/i);
    await expect(page.getByTestId('lobby-ai-roster')).toContainText('openai/fast-oracle');
    await expect(page.getByTestId('lobby-ai-roster')).toContainText('× 2');
    await expect(page.getByTestId('lobby-ai-roster')).not.toContainText(/public seat|seat 1|seat 2/i);

    // The server readiness projection, not a client-side member count, blocks
    // the host before a not-ready friend can advance the funded table.
    await expect(page.getByTestId('lobby-readiness-reasons')).toContainText(/ready up/i);
    await expect(page.getByTestId('lobby-start')).toBeDisabled();

    await expect(page.getByTestId('lobby-transfer-friend-member-safe')).toBeVisible();
    await expect(page.getByTestId('lobby-kick-friend-member-safe')).toBeVisible();
    await page.getByTestId('lobby-transfer-friend-member-safe').click();
    await expect(page.getByTestId('lobby-start')).toHaveCount(0);
    await expect(page.getByTestId('lobby-leave')).toBeVisible();
  });

  test('builds a custom casual table with a validated deck, duplicate model slots, and a bound quote', async ({
    page
  }) => {
    const isApi = (route: import('@playwright/test').Route) => {
      const t = route.request().resourceType();
      return t === 'fetch' || t === 'xhr';
    };
    let createBody: Record<string, unknown> | null = null;
    let updateBody: Record<string, unknown> | null = null;
    let launchBody: Record<string, unknown> | null = null;

    await routeRulesets(page);
    await page.route('**/human/models*', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          catalog: { content_hash: 'catalog-hash', fetched_at: null, state: 'current', is_stale: false },
          next_cursor: null,
          items: [
            {
              canonical_slug: 'openai/fast', provider: 'OpenAI', display_name: 'Fast Oracle', description: null,
              modalities: ['text'], context_length: 128000,
              pricing_per_million_micros: { prompt_per_million: 500000, completion_per_million: 1500000 },
              supported_capabilities: ['structured_output'], supports_structured_output: true,
              qualification_tier: 'verified_structured', qualification_reasons: [], casual_visible: true,
              privacy: 'zdr', availability: 'available', availability_reason: 'current', expires_at: null,
              platform_eligible: true, platform_ineligibility_reasons: [], observed_latency_ms: 700,
              observed_throughput_tokens_per_second: 120, open_weights: false, frontier: false,
              requires_byok: false, catalog_freshness: { content_hash: 'catalog-hash', fetched_at: null, state: 'current', is_stale: false }
            },
            {
              canonical_slug: 'acme/offline', provider: 'Acme', display_name: 'Offline Engine', description: null,
              modalities: ['text'], context_length: null, pricing_per_million_micros: {},
              supported_capabilities: [], supports_structured_output: false,
              qualification_tier: 'unverified', qualification_reasons: ['unavailable'], casual_visible: true,
              privacy: 'unknown', availability: 'unavailable', availability_reason: 'provider unavailable', expires_at: null,
              platform_eligible: false, platform_ineligibility_reasons: ['model_unavailable_before_lock'], observed_latency_ms: null,
              observed_throughput_tokens_per_second: null, open_weights: null, frontier: null,
              requires_byok: true, catalog_freshness: { content_hash: 'catalog-hash', fetched_at: null, state: 'current', is_stale: false }
            }
          ]
        })
      });
    });
    await page.route('**/human/wallet', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ granted_micros: 5000000, available_micros: 3000000, reserved_micros: 0, spent_micros: 2000000, completion_subsidy_micros: 0, recent_entries: [] }) });
    });
    await page.route('**/human/openrouter/connect', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ connected: false, label: null, display_suffix: null, validated_at: null, credit_capability: null }) });
    });
    await page.route('**/human/guest', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify({ principal_id: HOST_PRINCIPAL, kind: 'guest', display_name: null }) });
    });
    await page.route('**/human/consent', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ consented: true, required_versions: { TOS: '1', PRIVACY: '1', AGE_GATE: '1' } })
      });
    });
    await page.route('**/human/me', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ principal_id: HOST_PRINCIPAL, kind: 'guest', display_name: 'Host' })
      });
    });
    await page.route('**/lobbies', async (route) => {
      if (!isApi(route)) return route.continue();
      createBody = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify(lobbySummary()) });
    });
    await page.route(`**/lobbies/${LOBBY_ID}/configuration`, async (route) => {
      if (!isApi(route)) return route.continue();
      updateBody = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(lobbySummary()) });
    });
    await page.route(`**/lobbies/${LOBBY_ID}/funding-preflight`, async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          funded: true, allowed: true, funding_source: 'PLATFORM', reason: 'ok', reasons: [],
          reservation_micros: 240000, expected_total_micros: 240000, funding_quote_hash: 'q'.repeat(64),
          estimate_hash: 'e'.repeat(64), model_hash: 'm'.repeat(64), catalog_hash: 'c'.repeat(64),
          rules_hash: 'r'.repeat(64), funding_hash: 'f'.repeat(64), byok_max_estimate_micros: null
        })
      });
    });
    await page.route(`**/lobbies/${LOBBY_ID}/roster`, async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          id: LOBBY_ID,
          status: 'OPEN',
          member_count: 1,
          composition: { human_count: 1, ai_count: 6, total: 7 },
          launch_readiness: { allowed: true, reasons: [] },
          members: [{ member_id: 'host-member', display_label: 'Host', is_self: true, host: true, ready: true, present: true }]
        })
      });
    });
    await page.route(`**/lobbies/${LOBBY_ID}/lock`, async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(lobbySummary('LOCKED')) });
    });
    await page.route(`**/lobbies/${LOBBY_ID}/launch`, async (route) => {
      if (!isApi(route)) return route.continue();
      launchBody = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ lobby_id: LOBBY_ID, game_id: 'bound-custom-game', status: 'LAUNCHED', created: true }) });
    });
    await page.route(`**/lobbies/${LOBBY_ID}`, async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(lobbySummary()) });
    });

    await page.goto('/lobby');
    await expect(page.getByTestId('custom-table-builder')).toBeVisible();
    await expect(page.getByTestId('custom-table-builder')).toContainText(/casual/i);
    await expect(page.getByTestId('custom-table-builder')).not.toContainText(/ranked/i);

    await page.getByTestId('table-builder-custom-deck').click();
    await expect(page.getByTestId('table-builder-role-explanation')).toContainText(/never assigns a chosen person/i);
    await expect(page.getByTestId('table-builder-deck-validation')).toContainText(/add roles/i);
    await page.getByTestId('role-add-MAFIA_GOON').click();
    await page.getByTestId('role-add-MAFIA_GOON').click();
    await page.getByTestId('role-add-DETECTIVE').click();
    await page.getByTestId('role-add-DOCTOR').click();
    await page.getByTestId('role-add-VILLAGER').click();
    await page.getByTestId('role-add-VILLAGER').click();
    await page.getByTestId('role-add-VILLAGER').click();
    await expect(page.getByTestId('table-builder-deck-validation')).toContainText(/ready/i);

    await page.getByTestId('table-builder-human-slots').fill('2');
    await page.getByTestId('model-assign-openai-fast').click();
    await page.getByTestId('ai-slot-3').selectOption('openai/fast');
    await expect(page.getByTestId('ai-slot-2')).toHaveValue('openai/fast');
    await expect(page.getByTestId('ai-slot-3')).toHaveValue('openai/fast');
    await expect(page.getByTestId('model-unavailable-acme-offline')).toContainText(/unavailable/i);

    await page.getByTestId('table-builder-get-quote').click();
    await expect.poll(() => createBody).toMatchObject({
      custom_role_counts: { MAFIA_GOON: 2, DETECTIVE: 1, DOCTOR: 1, VILLAGER: 3 },
      human_seat_count: 2,
      ai_model_selections: [
        { seat_index: 2, model_slug: 'openai/fast', funding_source: 'platform' },
        { seat_index: 3, model_slug: 'openai/fast', funding_source: 'platform' }
      ]
    });
    await expect(page.getByTestId('table-builder-quote')).toContainText('$0.24');
    await expect(page.getByTestId('table-builder-quote')).toContainText(/bound/i);

    await page.getByTestId('table-builder-cadence').selectOption('builtin_sync_v1');
    await expect(page.getByTestId('table-builder-quote-stale')).toContainText(/refresh/i);
    await page.getByTestId('table-builder-get-quote').click();
    await expect.poll(() => updateBody).toMatchObject({ cadence_preset: 'builtin_sync_v1' });

    await page.getByTestId('table-builder-open-waiting-room').click();
    await expect(page).toHaveURL(new RegExp(`/lobby/${LOBBY_ID}\\?funding_quote_hash=${'q'.repeat(64)}`));
    await expect(page.getByTestId('lobby-funding-quote-bound')).toBeVisible();
    await page.getByTestId('lobby-start').click();
    await expect.poll(() => launchBody).toEqual({
      funding_quote_hash: 'q'.repeat(64),
      byok_max_estimate_micros: null
    });
    await expect(page).toHaveURL(/\/play\/bound-custom-game$/);
  });

  test('keeps disconnected BYOK and insufficient promotional credit errors inline', async ({ page }) => {
    const isApi = (route: import('@playwright/test').Route) => {
      const t = route.request().resourceType();
      return t === 'fetch' || t === 'xhr';
    };
    await routeRulesets(page);
    await page.route('**/human/models*', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ catalog: { content_hash: null, fetched_at: null, state: 'current', is_stale: false }, next_cursor: null, items: [] }) });
    });
    await page.route('**/human/wallet', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ granted_micros: 5000000, available_micros: 0, reserved_micros: 0, spent_micros: 5000000, completion_subsidy_micros: 0, recent_entries: [] }) });
    });
    await page.route('**/human/openrouter/connect', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ connected: false, label: null, display_suffix: null, validated_at: null, credit_capability: null }) });
    });
    await page.route('**/human/guest', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify({ principal_id: GUEST_PRINCIPAL, kind: 'guest', display_name: null }) });
    });

    await page.goto('/lobby');
    await expect(page.getByTestId('table-builder-promo-unavailable')).toContainText(/no promotional credit/i);
    await page.getByLabel('Use connected OpenRouter funding').check();
    await expect(page.getByTestId('table-builder-byok-disconnected')).toContainText(/connect your openrouter/i);
    expect((await page.getByTestId('custom-table-builder').textContent()) ?? '').not.toMatch(/token|api key|credential=/i);
  });
});
