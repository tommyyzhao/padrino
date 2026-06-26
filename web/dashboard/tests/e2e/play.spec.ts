import { expect, test, type Page, type Route } from '@playwright/test';
import { expectIdentityBlind } from './helpers/identityBlind';

// US-155: Frontend in-game play surface.
//
// /play/[gameId] is the interactive board: a seat board (identity-blind,
// count-only composition, themed static sprites), a non-precise phase
// countdown, legal-action-gated action / vote / night panels, and a buffered
// chat composer whose feed is fed ONLY by RELEASED frames.
//
// The smoke harness has no in-progress human game, so every transport surface
// is intercepted with deterministic fixtures (the watch.spec.ts SSE pattern):
//   * the live-tail `/public/games/{id}/live` stream emits released PUBLIC
//     frames (a scripted turn: a vote + one released chat message),
//   * the seat observation `/human/games/{id}/observation/stream` emits the
//     seat's legal actions + a transport-only phase-deadline frame,
//   * the composition endpoint serves counts-only,
//   * the action / chat POSTs accept idempotently.
//
// Anonymity (AGENTS.md rule 7) is asserted by scanning the rendered board for
// human-vs-AI / model-identity markers — there are none before the reveal.

const GAME_ID = 'dddd0004-0004-0004-0004-dddddddddddd';
const NAR_GAME_ID = 'eeee0004-0004-0004-0004-eeeeeeeeeeee';
const SEAT_ME = 'p1seataa-0000-0000-0000-000000000001';
const SEAT_OTHER = 'p2seatbb-0000-0000-0000-000000000002';
const SEAT_THIRD = 'p3seatcc-0000-0000-0000-000000000003';

type LiveBatch =
  | Record<string, unknown>[]
  | { frames: Record<string, unknown>[]; wait?: Promise<void> };

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

// A scripted public turn: a vote, then one released chat line.
const PUBLIC_FRAMES = [
  mkFrame(1, 'PhaseStarted', 'DAY_1_VOTE', null, {}),
  mkFrame(2, 'VoteSubmitted', 'DAY_1_VOTE', SEAT_OTHER, { target: SEAT_ME, is_abstain: false }),
  mkFrame(3, 'PublicMessageSubmitted', 'DAY_1_VOTE', SEAT_OTHER, { text: 'I think it is p1.' })
];

// The seat's own observation (legal actions for a VOTE phase) + a deadline frame
// far enough out that the countdown bucket is non-ending.
const OBSERVATION_FRAMES = [
  {
    type: 'observation',
    phase: 'DAY_1_VOTE',
    you: { player_id: SEAT_ME, alive: true },
    alive_players: [SEAT_ME, SEAT_OTHER],
    legal_actions: { allowed_action_types: ['VOTE', 'ABSTAIN'], legal_targets: [SEAT_OTHER] }
  },
  { type: 'phase_deadline', phase: 'DAY_1_VOTE', deadline_at: '2099-01-01T00:02:00Z' }
];

function observationSseBody(frames: Record<string, unknown>[] = OBSERVATION_FRAMES): string {
  return frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join('');
}

async function mockStandardPlaySurface(
  page: Page,
  {
    gameId = GAME_ID,
    publicFrames = PUBLIC_FRAMES,
    liveBatches,
    observationFrames = OBSERVATION_FRAMES,
    onAction,
    onChat
  }: {
    gameId?: string;
    publicFrames?: Record<string, unknown>[];
    liveBatches?: LiveBatch[];
    observationFrames?: Record<string, unknown>[];
    onAction?: (payload: Record<string, unknown>) => void;
    onChat?: (payload: Record<string, unknown>) => void;
  } = {}
): Promise<void> {
  const isApi = (route: Route) => {
    const t = route.request().resourceType();
    return t === 'fetch' || t === 'xhr';
  };

  await page.route(`**/public/games/${gameId}/composition`, async (route) => {
    if (!isApi(route)) return route.continue();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        game_id: gameId,
        ruleset_id: 'mini7_v1',
        composition: { human_count: 2, ai_count: 5, total: 7 }
      })
    });
  });

  let liveIndex = 0;
  await page.route(`**/public/games/${gameId}/live*`, async (route) => {
    const batches = liveBatches ?? [publicFrames];
    const batch = batches[liveIndex];
    liveIndex += 1;
    if (batch) {
      const frames = Array.isArray(batch) ? batch : batch.frames;
      if (!Array.isArray(batch) && batch.wait) await batch.wait;
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
        body: buildSseBody(frames)
      });
    } else {
      await route.abort('failed');
    }
  });

  let firstObs = true;
  await page.route(`**/human/games/${gameId}/observation/stream*`, async (route) => {
    if (firstObs) {
      firstObs = false;
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
        body: observationSseBody(observationFrames)
      });
    } else {
      await route.abort('failed');
    }
  });

  await page.route(`**/human/games/${gameId}/actions`, async (route) => {
    if (!isApi(route)) return route.continue();
    onAction?.(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        accepted: true,
        public_player_id: SEAT_ME,
        phase: 'DAY_1_VOTE',
        action_type: 'VOTE',
        target: SEAT_OTHER,
        idempotent_replay: false
      })
    });
  });

  await page.route(`**/human/games/${gameId}/chat`, async (route) => {
    if (!isApi(route)) return route.continue();
    onChat?.(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        accepted: true,
        public_player_id: SEAT_ME,
        phase: 'DAY_1_VOTE',
        channel: 'PUBLIC',
        status: 'RELEASED',
        idempotent_replay: false
      })
    });
  });
}

async function expectTouchSized(locator: ReturnType<Page['getByTestId']>): Promise<void> {
  const box = await locator.boundingBox();
  expect(box).not.toBeNull();
  expect(box?.height).toBeGreaterThanOrEqual(44);
}

test.describe('play surface (US-155)', () => {
  test('scripted turn: board + vote + a released chat message, identity-blind', async ({ page }) => {
    let actionPosted = false;
    let chatPosted = false;

    const isApi = (route: import('@playwright/test').Route) => {
      const t = route.request().resourceType();
      return t === 'fetch' || t === 'xhr';
    };

    // Counts-only composition (never a per-seat human/AI map).
    await page.route(`**/public/games/${GAME_ID}/composition`, async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          game_id: GAME_ID,
          ruleset_id: 'mini7_v1',
          composition: { human_count: 2, ai_count: 5, total: 7 }
        })
      });
    });

    // Live-tail released PUBLIC frames.
    let firstLive = true;
    await page.route(`**/public/games/${GAME_ID}/live*`, async (route) => {
      if (firstLive) {
        firstLive = false;
        await route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
          body: buildSseBody(PUBLIC_FRAMES)
        });
      } else {
        await route.abort('failed');
      }
    });

    // Seat observation snapshot (legal actions + deadline).
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

    // Action POST accepts idempotently.
    await page.route(`**/human/games/${GAME_ID}/actions`, async (route) => {
      if (!isApi(route)) return route.continue();
      actionPosted = true;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          accepted: true,
          public_player_id: SEAT_ME,
          phase: 'DAY_1_VOTE',
          action_type: 'VOTE',
          target: SEAT_OTHER,
          idempotent_replay: false
        })
      });
    });

    // Chat POST: the message enters the hold and is RELEASED (stub-pass gate).
    await page.route(`**/human/games/${GAME_ID}/chat`, async (route) => {
      if (!isApi(route)) return route.continue();
      chatPosted = true;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          accepted: true,
          public_player_id: SEAT_ME,
          phase: 'DAY_1_VOTE',
          channel: 'PUBLIC',
          status: 'RELEASED',
          idempotent_replay: false
        })
      });
    });

    await page.goto(`/play/${GAME_ID}`);
    await expect(page.getByTestId('play-title')).toBeVisible();

    await page.getByTestId('play-help-open').click();
    const help = page.getByTestId('play-help-drawer');
    await expect(help).toBeVisible();
    await expect(help.getByTestId('how-to-play-panel')).toContainText('How to play Mafia');
    await expect(help.getByTestId('how-to-play-day')).toContainText('vote');
    await expect(help.getByTestId('how-to-play-win')).toContainText('Town wins');
    await expect(help.getByTestId('how-to-play-spot-ai')).toContainText('Spot the AI');
    await expectIdentityBlind(help);
    await page.getByTestId('play-help-close').click();
    await expect(help).toHaveCount(0);

    // Composition is counts-only.
    await expect(page.getByTestId('play-composition')).toContainText('2 humans');
    await expect(page.getByTestId('play-composition')).toContainText('5 AI');

    // The countdown header is non-precise (a coarse bucket, never exact seconds).
    await expect(page.getByTestId('play-countdown')).toBeVisible();
    const countdownText = await page.getByTestId('play-countdown').textContent();
    expect(countdownText).not.toMatch(/\d+\s*s/i);

    // The board renders seats from released frames, with themed sprites.
    await expect(page.getByTestId('play-seat-grid')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('play-seat-sprite').first()).toBeVisible();

    // The released chat message appears in the feed.
    await expect(page.getByTestId('play-chat-feed')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('play-chat-line')).toContainText('I think it is p1.');

    // The vote panel is legal-action-gated and submits a vote.
    await expect(page.getByTestId('play-vote-panel')).toBeVisible({ timeout: 15_000 });
    await page.getByTestId('play-vote-target').selectOption(SEAT_OTHER);
    await page.getByTestId('play-vote-submit').click();
    await expect(page.getByTestId('play-action-note')).toContainText('Vote submitted', {
      timeout: 15_000
    });
    expect(actionPosted).toBe(true);

    // The buffered chat composer holds then shows the released state.
    await page.getByTestId('play-chat-input').fill('hello friends');
    await page.getByTestId('play-chat-send').click();
    await expect(page.getByTestId('play-chat-status')).toHaveAttribute('data-status', 'released', {
      timeout: 15_000
    });
    expect(chatPosted).toBe(true);

    // Anonymity: the rendered board exposes no human/AI or model markers.
    await expectIdentityBlind(page.getByTestId('play-shell'));
  });

  test('server-driven NAR night action: presents ROLEBLOCK and submits its target', async ({
    page
  }) => {
    let actionPayload: Record<string, unknown> | null = null;

    const isApi = (route: import('@playwright/test').Route) => {
      const t = route.request().resourceType();
      return t === 'fetch' || t === 'xhr';
    };

    await page.route(`**/public/games/${NAR_GAME_ID}/composition`, async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          game_id: NAR_GAME_ID,
          ruleset_id: 'roleblock10_v1',
          composition: { human_count: 1, ai_count: 9, total: 10 }
        })
      });
    });

    let firstLive = true;
    await page.route(`**/public/games/${NAR_GAME_ID}/live*`, async (route) => {
      if (firstLive) {
        firstLive = false;
        await route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
          body: buildSseBody([
            mkFrame(1, 'PhaseStarted', 'NIGHT_1_ACTIONS', null, {}),
            mkFrame(2, 'PublicMessageSubmitted', 'DAY_1_DISCUSSION_ROUND_1', SEAT_OTHER, {
              text: 'public setup'
            })
          ])
        });
      } else {
        await route.abort('failed');
      }
    });

    const narObservationFrames = [
      {
        type: 'observation',
        phase: 'NIGHT_1_ACTIONS',
        you: { player_id: SEAT_ME, alive: true },
        alive_players: [SEAT_ME, SEAT_OTHER, SEAT_THIRD],
        legal_actions: {
          allowed_action_types: ['ROLEBLOCK'],
          legal_targets: [SEAT_OTHER, SEAT_THIRD],
          action_descriptions: {
            ROLEBLOCK: 'Block one legal target from completing their night action.'
          }
        }
      },
      { type: 'phase_deadline', phase: 'NIGHT_1_ACTIONS', deadline_at: '2099-01-01T00:02:00Z' }
    ];

    let firstObs = true;
    await page.route(`**/human/games/${NAR_GAME_ID}/observation/stream*`, async (route) => {
      if (firstObs) {
        firstObs = false;
        await route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
          body: observationSseBody(narObservationFrames)
        });
      } else {
        await route.abort('failed');
      }
    });

    await page.route(`**/human/games/${NAR_GAME_ID}/actions`, async (route) => {
      if (!isApi(route)) return route.continue();
      actionPayload = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          accepted: true,
          public_player_id: SEAT_ME,
          phase: 'NIGHT_1_ACTIONS',
          action_type: 'ROLEBLOCK',
          target: SEAT_THIRD,
          idempotent_replay: false
        })
      });
    });

    await page.goto(`/play/${NAR_GAME_ID}`);
    await expect(page.getByTestId('play-night-panel')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('play-night-action-type')).toContainText('Roleblock');
    await expect(page.getByTestId('play-night-action-description')).toContainText(
      'Block one legal target from completing their night action.'
    );

    await page.getByTestId('play-night-target').selectOption(SEAT_THIRD);
    await page.getByTestId('play-night-submit').click();
    await expect(page.getByTestId('play-action-note')).toContainText('Roleblock submitted', {
      timeout: 15_000
    });

    expect(actionPayload).not.toBeNull();
    expect(actionPayload?.['action']).toMatchObject({ type: 'ROLEBLOCK', target: SEAT_THIRD });
  });

  test('help drawer renders identity-blind rules on a phone-width viewport', async ({ page }) => {
    const isApi = (route: import('@playwright/test').Route) => {
      const t = route.request().resourceType();
      return t === 'fetch' || t === 'xhr';
    };

    await page.setViewportSize({ width: 390, height: 844 });

    await page.route(`**/public/games/${GAME_ID}/composition`, async (route) => {
      if (!isApi(route)) return route.continue();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          game_id: GAME_ID,
          ruleset_id: 'mini7_v1',
          composition: { human_count: 2, ai_count: 5, total: 7 }
        })
      });
    });

    let firstLive = true;
    await page.route(`**/public/games/${GAME_ID}/live*`, async (route) => {
      if (firstLive) {
        firstLive = false;
        await route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          headers: { 'Cache-Control': 'no-cache', Connection: 'keep-alive' },
          body: buildSseBody(PUBLIC_FRAMES)
        });
      } else {
        await route.abort('failed');
      }
    });

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

    await page.goto(`/play/${GAME_ID}`);
    await page.getByTestId('play-help-open').click();
    const help = page.getByTestId('play-help-drawer');
    await expect(help).toBeVisible();
    await expect(help.getByTestId('how-to-play-panel')).toBeVisible();
    await expect(help.getByTestId('how-to-play-night')).toBeVisible();
    await expect(help).toHaveCSS('overflow-y', 'auto');
    await expectIdentityBlind(help);
  });
});

test.describe('play vote tally (US-283)', () => {
  test('renders and live-updates voter rows and running target counts without identity leaks', async ({
    page
  }) => {
    let releaseUpdate!: () => void;
    const updateGate = new Promise<void>((resolve) => {
      releaseUpdate = resolve;
    });

    await mockStandardPlaySurface(page, {
      liveBatches: [
        PUBLIC_FRAMES,
        {
          wait: updateGate,
          frames: [
            mkFrame(4, 'VoteSubmitted', 'DAY_1_VOTE', SEAT_THIRD, {
              target: SEAT_ME,
              is_abstain: false
            })
          ]
        }
      ],
      observationFrames: [
        {
          type: 'observation',
          phase: 'DAY_1_VOTE',
          you: { player_id: SEAT_ME, alive: true },
          alive_players: [SEAT_ME, SEAT_OTHER, SEAT_THIRD],
          legal_actions: { allowed_action_types: ['VOTE', 'ABSTAIN'], legal_targets: [SEAT_OTHER] }
        },
        { type: 'phase_deadline', phase: 'DAY_1_VOTE', deadline_at: '2099-01-01T00:02:00Z' }
      ]
    });

    await page.goto(`/play/${GAME_ID}`);

    const tally = page.getByTestId('play-vote-tally-panel');
    await expect(tally).toBeVisible({ timeout: 15_000 });
    await expectIdentityBlind(tally);

    const targetRow = page.locator(
      `[data-testid="play-vote-count-row"][data-target="${SEAT_ME}"]`
    );
    const firstVoterRow = page.locator(
      `[data-testid="play-vote-voter-row"][data-voter="${SEAT_OTHER}"]`
    );
    await expect(targetRow).toHaveAttribute('data-count', '1');
    await expect(firstVoterRow).toHaveAttribute('data-target', SEAT_ME);

    releaseUpdate();

    await expect(targetRow).toHaveAttribute('data-count', '2', { timeout: 15_000 });
    await expect(
      page.locator(`[data-testid="play-vote-voter-row"][data-voter="${SEAT_THIRD}"]`)
    ).toHaveAttribute('data-target', SEAT_ME);
  });

  test('is reachable on a phone-width actions panel', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await mockStandardPlaySurface(page);

    await page.goto(`/play/${GAME_ID}`);
    await page.getByTestId('play-mobile-tab-actions').click();

    const tally = page.getByTestId('play-vote-tally-panel');
    await expect(tally).toBeVisible({ timeout: 15_000 });
    await expect(tally).toBeInViewport();
    await expect(page.getByTestId('play-vote-count-row')).toHaveCount(1);
  });

  test('hides when the live stream leaves the day vote phase', async ({ page }) => {
    let releaseNight!: () => void;
    const nightGate = new Promise<void>((resolve) => {
      releaseNight = resolve;
    });

    await mockStandardPlaySurface(page, {
      liveBatches: [
        PUBLIC_FRAMES,
        {
          wait: nightGate,
          frames: [mkFrame(4, 'PhaseStarted', 'NIGHT_1_ACTIONS', null, {})]
        }
      ]
    });

    await page.goto(`/play/${GAME_ID}`);
    await expect(page.getByTestId('play-vote-tally-panel')).toBeVisible({ timeout: 15_000 });

    releaseNight();

    await expect(page.getByTestId('play-vote-tally-panel')).toHaveCount(0, {
      timeout: 15_000
    });
  });
});

test.describe('play surface responsive layout (US-282)', () => {
  test('phone-width tabs make board, chat, and actions reachable with touch targets', async ({
    page
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await mockStandardPlaySurface(page);

    await page.goto(`/play/${GAME_ID}`);
    await expect(page.getByTestId('play-title')).toBeVisible();

    const tabs = page.getByTestId('play-mobile-tabs');
    await expect(tabs).toBeVisible();
    await expectTouchSized(page.getByTestId('play-mobile-tab-board'));
    await expectTouchSized(page.getByTestId('play-mobile-tab-chat'));
    await expectTouchSized(page.getByTestId('play-mobile-tab-actions'));

    await expect(page.getByTestId('play-mobile-tab-board')).toHaveAttribute(
      'aria-selected',
      'true'
    );
    await expect(page.getByTestId('play-seat-grid')).toBeVisible({ timeout: 15_000 });

    await page.getByTestId('play-mobile-tab-chat').click();
    await expect(page.getByTestId('play-mobile-tab-chat')).toHaveAttribute(
      'aria-selected',
      'true'
    );
    await expect(page.getByTestId('play-chat-feed')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('play-chat-line')).toContainText('I think it is p1.');

    await page.getByTestId('play-mobile-tab-actions').click();
    await expect(page.getByTestId('play-mobile-tab-actions')).toHaveAttribute(
      'aria-selected',
      'true'
    );
    await expect(page.getByTestId('play-vote-panel')).toBeVisible({ timeout: 15_000 });
    await expectTouchSized(page.getByTestId('play-vote-submit'));
  });

  test('wide viewport keeps the three-column desktop play layout', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await mockStandardPlaySurface(page);

    await page.goto(`/play/${GAME_ID}`);
    await expect(page.getByTestId('play-seat-grid')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('play-mobile-tabs')).toBeHidden();

    const shell = page.getByTestId('play-shell');
    await expect(shell).toHaveCSS('display', 'grid');
    const columns = await shell.evaluate((el) => getComputedStyle(el).gridTemplateColumns);
    expect(columns).toContain('220px');
    expect(columns).toContain('300px');

    const boardBox = await page.getByTestId('play-board-panel').boundingBox();
    const mainBox = await page.getByTestId('play-main-panel').boundingBox();
    const infoBox = await page.getByTestId('play-info-panel').boundingBox();
    expect(boardBox?.x ?? 0).toBeLessThan(mainBox?.x ?? 0);
    expect(mainBox?.x ?? 0).toBeLessThan(infoBox?.x ?? 0);
  });

  test('mobile chat composer stays reachable and submits from a narrow viewport', async ({
    page
  }) => {
    let chatPayload: Record<string, unknown> | null = null;
    await page.setViewportSize({ width: 390, height: 844 });
    await mockStandardPlaySurface(page, {
      onChat: (payload) => {
        chatPayload = payload;
      }
    });

    await page.goto(`/play/${GAME_ID}`);
    await page.getByTestId('play-mobile-tab-chat').click();

    const input = page.getByTestId('play-chat-input');
    await expect(input).toBeVisible({ timeout: 15_000 });
    await expect(input).toBeInViewport();
    await input.fill('hello from mobile');
    await page.getByTestId('play-chat-send').click();

    await expect(page.getByTestId('play-chat-status')).toHaveAttribute('data-status', 'released', {
      timeout: 15_000
    });
    expect(chatPayload).toMatchObject({ channel: 'PUBLIC', text: 'hello from mobile' });
  });
});
