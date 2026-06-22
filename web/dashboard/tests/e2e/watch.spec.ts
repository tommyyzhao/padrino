import { expect, test } from '@playwright/test';
import { expectIdentityBlind, ROLE_AND_FACTION_TOKENS } from './helpers/identityBlind';

// US-091: consumer live-viewer page.
//
// The SSE endpoint is intercepted with page.route so these tests run without a
// live backend.  Two scenarios cover the key acceptance criteria:
//   1. Events without a terminal frame → phase / chat / seats / votes render,
//      outcome banner is ABSENT (spoiler safety).
//   2. Events ending with GameTerminated → outcome banner appears with winner.

const GAME_ID = '11111111-2222-3333-4444-aaaaaaaaaaaa';
const NAR_GAME_ID = '22222222-3333-4444-5555-bbbbbbbbbbbb';
const ALICE_ID = 'aaaaaaaa-bbbb-cccc-dddd-ee0000000000';
const BOB_ID = 'bbbbbbbb-cccc-dddd-eeee-ff0000000000';
const CLEANED_ID = 'cccccccc-dddd-eeee-ffff-000000000000';

function mkFrame(
  seq: number,
  eventType: string,
  phase: string,
  actorPlayerId: string | null,
  payload: Record<string, unknown>
): Record<string, unknown> {
  return {
    schema_version: 'public_event_v1',
    sequence: seq,
    event_type: eventType,
    phase,
    visibility: 'PUBLIC',
    actor_player_id: actorPlayerId,
    payload,
    prev_event_hash: `h${seq - 1}`,
    event_hash: `h${seq}`
  };
}

function buildSseBody(frames: Record<string, unknown>[]): string {
  return frames
    .map((f) => `id: ${f['sequence'] as number}\ndata: ${JSON.stringify(f)}\n\n`)
    .join('');
}

const BASE_FRAMES = [
  mkFrame(1, 'PhaseStarted', 'Day 1', null, {}),
  mkFrame(2, 'PublicMessageSubmitted', 'Day 1', ALICE_ID, { text: 'Hello everyone!' }),
  mkFrame(3, 'VoteSubmitted', 'Day 1', ALICE_ID, { target: BOB_ID, is_abstain: false }),
  mkFrame(4, 'PlayerEliminated', 'Day 1', null, {
    public_player_id: BOB_ID,
    cause: 'DAY_VOTE'
  })
];

const NAR_NIGHT_FRAMES = [
  mkFrame(1, 'PhaseStarted', 'NIGHT_1_ACTIONS', null, {}),
  mkFrame(2, 'PublicMessageSubmitted', 'DAY_1_DISCUSSION_ROUND_1', ALICE_ID, {
    text: 'Keep the votes structured.'
  }),
  mkFrame(3, 'PlayerEliminated', 'NIGHT_1_ACTIONS', null, {
    public_player_id: CLEANED_ID,
    cause: 'night_kill'
  })
];

const TERMINAL_FRAME = mkFrame(5, 'GameTerminated', 'End', null, {
  winner: 'TOWN',
  reason: 'all_mafia_eliminated'
});

test.describe('watch', () => {
  test('renders phase, chat, seats, and vote tally; hides outcome before terminal', async ({
    page
  }) => {
    let firstRequest = true;
    await page.route(`**/public/games/${GAME_ID}/live*`, async (route) => {
      if (firstRequest) {
        firstRequest = false;
        await route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
          body: buildSseBody(BASE_FRAMES)
        });
      } else {
        // Abort reconnects to prevent duplicate events
        await route.abort('failed');
      }
    });

    await page.goto(`/watch/${GAME_ID}`);
    await expect(page.getByTestId('watch-title')).toBeVisible();

    // Wait for events to be processed (chat is a reliable signal)
    await expect(page.getByTestId('watch-chat-feed')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('watch-chat-entry')).toHaveCount(1);

    // Phase is rendered
    await expect(page.getByTestId('watch-phase')).toHaveText('Day 1');

    // Seat grid shows both players; bob is dead
    await expect(page.getByTestId('watch-seat-row')).toHaveCount(2);
    await expect(page.locator('[data-testid="watch-seat-row"][data-alive="false"]')).toHaveCount(
      1
    );
    await expect(
      page.locator(`[data-testid="watch-seat-row"][data-player-id="${BOB_ID}"][data-alive="false"]`)
    ).toHaveCount(1);

    // Vote tally is present (alice voted for bob)
    await expect(page.getByTestId('watch-vote-tally')).toBeVisible();
    await expect(page.getByTestId('watch-vote-row')).toHaveCount(1);

    // No outcome banner (spoiler safety)
    await expect(page.getByTestId('watch-outcome-banner')).toHaveCount(0);
    await expectIdentityBlind(page.getByTestId('watch-shell'));
  });

  test('renders projected NAR night death from payload only and stays identity-blind', async ({
    page
  }) => {
    let firstRequest = true;
    await page.route(`**/public/games/${NAR_GAME_ID}/live*`, async (route) => {
      if (firstRequest) {
        firstRequest = false;
        await route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
          body: buildSseBody(NAR_NIGHT_FRAMES)
        });
      } else {
        await route.abort('failed');
      }
    });

    await page.goto(`/watch/${NAR_GAME_ID}`);
    await expect(page.getByTestId('watch-title')).toBeVisible();

    await expect(page.getByTestId('watch-night-outcomes')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('watch-night-outcome')).toHaveCount(1);
    await expect(page.getByTestId('watch-night-outcome')).toContainText(
      CLEANED_ID.slice(0, 8)
    );
    await expect(
      page.locator(`[data-testid="watch-seat-row"][data-player-id="${CLEANED_ID}"]`)
    ).toHaveAttribute('data-alive', 'false');

    await expectIdentityBlind(page.getByTestId('watch-shell'), ROLE_AND_FACTION_TOKENS);
  });

  test('shows outcome banner after terminal frame streams', async ({ page }) => {
    let firstRequest = true;
    await page.route(`**/public/games/${GAME_ID}/live*`, async (route) => {
      if (firstRequest) {
        firstRequest = false;
        await route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
          body: buildSseBody([...BASE_FRAMES, TERMINAL_FRAME])
        });
      } else {
        await route.abort('failed');
      }
    });

    await page.goto(`/watch/${GAME_ID}`);
    await expect(page.getByTestId('watch-title')).toBeVisible();

    // Outcome banner appears after GameTerminated
    await expect(page.getByTestId('watch-outcome-banner')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('watch-winner')).toHaveText('TOWN');

    // Status badge shows Ended
    await expect(page.getByTestId('watch-status')).toHaveText('Ended');
  });
});
