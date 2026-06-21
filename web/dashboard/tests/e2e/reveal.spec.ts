import { expect, test } from '@playwright/test';

// US-156: Frontend endgame reveal + spot-the-AI guess + profile/stats.
//
// /play/[gameId]/reveal is the post-terminal private human-game surface:
//   * the spot-the-AI guess UI (per-seat HUMAN/AI toggles + single submit) is
//     shown FIRST, gating the reveal of the viewer's OWN detection accuracy,
//   * after submitting the guess, the canonical endgame reveal is disclosed:
//     per-seat human/AI, role, faction, exact model, takeover provenance, and a
//     themed sprite,
//   * the viewer's personal detection accuracy is then visible.
//
// /profile is a minimal signed-in stats page (casual framing, no live ELO).
//
// The smoke harness has no terminal human game, so every endpoint is
// intercepted with deterministic fixtures (the play.spec.ts pattern).

const GAME_ID = 'eeee0005-0005-0005-0005-eeeeeeeeeeee';
const SEAT_ME = 'p1seataa-0000-0000-0000-000000000001';
const SEAT_HUMAN = 'p2seatbb-0000-0000-0000-000000000002';
const SEAT_AI = 'p3seatcc-0000-0000-0000-000000000003';
const SEAT_TAKEOVER = 'p4seatdd-0000-0000-0000-000000000004';

const REVEAL_BODY = {
  game_id: GAME_ID,
  ruleset_id: 'mini7_v1',
  winner: 'TOWN',
  seats: [
    {
      public_player_id: SEAT_ME,
      seat_index: 0,
      is_human: true,
      role: 'VILLAGER',
      faction: 'TOWN',
      alive: true,
      takeover_provenance: 'HUMAN',
      taken_over_at_phase: null,
      model: null
    },
    {
      public_player_id: SEAT_HUMAN,
      seat_index: 1,
      is_human: true,
      role: 'DETECTIVE',
      faction: 'TOWN',
      alive: false,
      takeover_provenance: 'HUMAN',
      taken_over_at_phase: null,
      model: null
    },
    {
      public_player_id: SEAT_AI,
      seat_index: 2,
      is_human: false,
      role: 'MAFIA',
      faction: 'MAFIA',
      alive: true,
      takeover_provenance: 'AI',
      taken_over_at_phase: null,
      model: {
        provider: 'cerebras',
        model_name: 'zai-glm-4.7',
        model_version: 'v1',
        agent_build_id: 'aaaaaaaa-0000-0000-0000-000000000000',
        display_name: 'GLM 4.7'
      }
    },
    {
      public_player_id: SEAT_TAKEOVER,
      seat_index: 3,
      is_human: false,
      role: 'DOCTOR',
      faction: 'TOWN',
      alive: true,
      takeover_provenance: 'HUMAN_THEN_AI',
      taken_over_at_phase: 'DAY_2_DISCUSSION',
      model: {
        provider: 'deepinfra',
        model_name: 'DeepSeek-V4-Flash',
        model_version: null,
        agent_build_id: 'bbbbbbbb-0000-0000-0000-000000000000',
        display_name: null
      }
    }
  ]
};

const GUESS_RESULT = {
  guesser_public_id: SEAT_ME,
  total: 3,
  correct: 2,
  accuracy: '0.6667',
  idempotent_replay: false
};

const isApi = (route: import('@playwright/test').Route) => {
  const t = route.request().resourceType();
  return t === 'fetch' || t === 'xhr';
};

// The viewer's own seat is resolved from the per-seat observation snapshot
// (the play-surface pattern); the reveal excludes it from the guessable list.
const OBSERVATION_FRAMES = [
  { type: 'observation', phase: 'GAME_OVER', you: { player_id: SEAT_ME, alive: true } }
];

function observationSseBody(): string {
  return OBSERVATION_FRAMES.map((f) => `data: ${JSON.stringify(f)}\n\n`).join('');
}

test.describe('endgame reveal + guess (US-156/US-163)', () => {
  test('private anonymous game: participant reaches guess-gated reveal', async ({ page }) => {
    let guessPosted: Record<string, string> | null = null;
    let guessFetchedBeforeSubmit = false;
    let guessSubmitted = false;
    let humanRevealRequested = false;
    let publicRevealRequested = false;

    // Before the guess is submitted, GET turing-guess is 404 (no guess yet).
    // After submit, the accuracy is available.
    await page.route(`**/human/games/${GAME_ID}/turing-guess`, async (route) => {
      if (!isApi(route)) return route.continue();
      const method = route.request().method();
      if (method === 'POST') {
        guessSubmitted = true;
        guessPosted = JSON.parse(route.request().postData() ?? '{}').guess as Record<string, string>;
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(GUESS_RESULT)
        });
        return;
      }
      // GET
      if (!guessSubmitted) {
        guessFetchedBeforeSubmit = true;
        await route.fulfill({
          status: 404,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'guess_required' })
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(GUESS_RESULT)
      });
    });

    // The viewer's own seat observation snapshot (resolves "my seat").
    let firstObs = true;
    await page.route(`**/human/games/${GAME_ID}/observation/stream*`, async (route) => {
      if (firstObs) {
        firstObs = false;
        await route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
          body: observationSseBody()
        });
      } else {
        await route.abort('failed');
      }
    });

    // The participant-gated canonical reveal is available once the private
    // anonymous human game is terminal.
    await page.route(`**/human/games/${GAME_ID}/reveal`, async (route) => {
      if (!isApi(route)) return route.continue();
      humanRevealRequested = true;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(REVEAL_BODY)
      });
    });
    await page.route(`**/public/games/${GAME_ID}/reveal`, async (route) => {
      if (!isApi(route)) return route.continue();
      publicRevealRequested = true;
      await route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'reveal_not_available' })
      });
    });

    await page.goto(`/play/${GAME_ID}/reveal`);
    await expect(page.getByTestId('reveal-title')).toBeVisible();

    // Before the guess, the reveal + accuracy are GATED (hidden).
    await expect(page.getByTestId('guess-panel')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('reveal-board')).toHaveCount(0);
    await expect(page.getByTestId('reveal-accuracy')).toHaveCount(0);

    // Per-seat HUMAN/AI toggles exist for every non-self seat.
    const toggleRows = page.getByTestId('guess-seat-row');
    await expect(toggleRows).toHaveCount(3);

    // Make a guess: SEAT_HUMAN -> HUMAN, SEAT_AI -> AI, SEAT_TAKEOVER -> AI.
    await page.getByTestId(`guess-${SEAT_HUMAN}-HUMAN`).click();
    await page.getByTestId(`guess-${SEAT_AI}-AI`).click();
    await page.getByTestId(`guess-${SEAT_TAKEOVER}-AI`).click();

    // Single submit.
    await page.getByTestId('guess-submit').click();

    // The accuracy is now disclosed (gated behind the guess).
    await expect(page.getByTestId('reveal-accuracy')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('reveal-accuracy')).toContainText('2');
    await expect(page.getByTestId('reveal-accuracy')).toContainText('3');

    // The reveal board is now disclosed with full per-seat truth.
    await expect(page.getByTestId('reveal-board')).toBeVisible({ timeout: 15_000 });
    const seatRows = page.getByTestId('reveal-seat-row');
    await expect(seatRows).toHaveCount(4);

    // The AI seat exposes its exact model.
    const aiRow = page.locator(`[data-testid="reveal-seat-row"][data-player-id="${SEAT_AI}"]`);
    await expect(aiRow).toContainText('cerebras');
    await expect(aiRow).toContainText('MAFIA');
    await expect(aiRow.getByTestId('reveal-seat-kind')).toContainText('AI');

    // The human seat is disclosed as human.
    const humanRow = page.locator(`[data-testid="reveal-seat-row"][data-player-id="${SEAT_HUMAN}"]`);
    await expect(humanRow.getByTestId('reveal-seat-kind')).toContainText('Human');

    // The takeover seat exposes its provenance.
    const takeoverRow = page.locator(
      `[data-testid="reveal-seat-row"][data-player-id="${SEAT_TAKEOVER}"]`
    );
    await expect(takeoverRow).toContainText('HUMAN_THEN_AI');

    // Each seat carries a themed sprite.
    await expect(page.getByTestId('reveal-seat-sprite').first()).toBeVisible();

    // The guess payload was submitted once with the chosen per-seat values.
    expect(guessSubmitted).toBe(true);
    expect(guessFetchedBeforeSubmit).toBe(true);
    expect(humanRevealRequested).toBe(true);
    expect(publicRevealRequested).toBe(false);
    expect(guessPosted).toEqual({
      [SEAT_HUMAN]: 'HUMAN',
      [SEAT_AI]: 'AI',
      [SEAT_TAKEOVER]: 'AI'
    });
  });
});

test.describe('profile / stats page (US-156)', () => {
  test('signed-in stats render with casual framing and no live ELO', async ({ page }) => {
    // A signed-in account.
    await page.route('**/human/me', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          principal_id: 'cccccccc-0000-0000-0000-000000000000',
          kind: 'account',
          display_name: 'Alice'
        })
      });
    });

    await page.route('**/human/stats*', async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ruleset_id: 'mini7_v1',
          principal_id: 'cccccccc-0000-0000-0000-000000000000',
          games: 10,
          wins: 6,
          draws: 1,
          losses: 3,
          role_win_rates: [{ role: 'VILLAGER', games: 5, wins: 3, rate: 0.6 }],
          survival_rate: 0.5,
          voting_accuracy: { total_votes: 8, accurate_votes: 5, rate: 0.625 },
          detection_accuracy: '0.7500'
        })
      });
    });

    await page.goto('/profile');
    await expect(page.getByTestId('profile-title')).toBeVisible();

    // Display name from the signed-in account.
    await expect(page.getByTestId('profile-display-name')).toContainText('Alice');

    // Core stats render.
    await expect(page.getByTestId('profile-stats')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('profile-games')).toContainText('10');
    await expect(page.getByTestId('profile-detection-accuracy')).toContainText('75');

    // Casual framing: no live ELO / rating / leaderboard wording.
    const html = (await page.getByTestId('profile-stats').innerHTML()).toLowerCase();
    expect(html).not.toContain('elo');
    expect(html).not.toContain('rating');
    expect(html).not.toContain('leaderboard');
  });
});
