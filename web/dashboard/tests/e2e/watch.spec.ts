import { expect, test, type Page } from '@playwright/test';
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

  test('shows every roster seat from the first frame (US-410)', async ({ page }) => {
    const seatIds = Array.from({ length: 7 }, (_, i) => `P0${i + 1}`);
    await page.route(`**/public/games/${GAME_ID}/seats`, (route) =>
      // The API is cross-origin: answer CORS preflight and allow the origin.
      route.fulfill({
        status: route.request().method() === 'OPTIONS' ? 204 : 200,
        contentType: 'application/json',
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Headers': '*'
        },
        body:
          route.request().method() === 'OPTIONS'
            ? ''
            : JSON.stringify({ game_id: GAME_ID, player_count: 7, seats: seatIds })
      })
    );
    let firstRequest = true;
    await page.route(`**/public/games/${GAME_ID}/live*`, async (route) => {
      if (firstRequest) {
        firstRequest = false;
        // Only P01 has acted: seat list must still be the full roster.
        await route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          body: buildSseBody([
            mkFrame(1, 'PublicMessageSubmitted', 'DAY_1', 'P01', { text: 'Hi.' })
          ])
        });
      } else {
        await route.abort('failed');
      }
    });

    await page.goto(`/watch/${GAME_ID}`);
    await expect(page.getByTestId('watch-chat-entry')).toHaveCount(1, { timeout: 15_000 });
    await expect(page.getByTestId('watch-seat-row')).toHaveCount(7);
    await expect(page.getByTestId('watch-alive-count')).toContainText('7 alive');
    await expectIdentityBlind(page.getByTestId('watch-shell'));
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

// US-411: narrated live watch + record mode. Audio is stubbed; tests end clips by hand.
const NARR_ID = '33333333-4444-5555-6666-cccccccccccc';
const P1 = 'P01';
const P2 = 'P02';

const NARR_FRAMES = [
  mkFrame(1, 'PhaseStarted', 'DAY_1', null, {}),
  mkFrame(2, 'PublicMessageSubmitted', 'DAY_1', P1, { text: 'First line.' }),
  mkFrame(3, 'PublicMessageSubmitted', 'DAY_1', P2, { text: 'Second line.' }),
  mkFrame(4, 'PlayerEliminated', 'DAY_1', null, { public_player_id: P2, cause: 'DAY_VOTE' }),
  mkFrame(5, 'GameTerminated', 'END', null, { winner: 'TOWN', reason: 'all_mafia_eliminated' })
];

async function stubAudio(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const w = window as unknown as { __audio: HTMLMediaElement[]; __ended: Set<HTMLMediaElement> };
    w.__audio = [];
    w.__ended = new Set();
    // No network: the stub keeps the URL on a data attribute instead of loading it.
    (window as unknown as { Audio: unknown }).Audio = function (url: string) {
      const el = document.createElement('audio');
      el.dataset.url = url;
      return el;
    };
    HTMLMediaElement.prototype.play = function (this: HTMLMediaElement) {
      w.__audio.push(this);
      return Promise.resolve();
    };
  });
}

/** Wait for a not-yet-finished clip, then finish it with `ended` or `error`. */
async function finishClip(page: Page, kind: 'ended' | 'error' = 'ended'): Promise<void> {
  await page.waitForFunction(() => {
    const w = window as unknown as { __audio: HTMLMediaElement[]; __ended: Set<HTMLMediaElement> };
    return w.__audio.some((a) => !w.__ended.has(a));
  });
  await page.evaluate((k) => {
    const w = window as unknown as { __audio: HTMLMediaElement[]; __ended: Set<HTMLMediaElement> };
    const a = w.__audio.find((x) => !w.__ended.has(x))!;
    w.__ended.add(a);
    a.dispatchEvent(new Event(k));
  }, kind);
}

async function mockLive(
  page: Page,
  handler: (url: URL, n: number) => Record<string, unknown>[] | null
): Promise<URL[]> {
  const urls: URL[] = [];
  await page.route(`**/public/games/${NARR_ID}/live*`, async (route) => {
    const url = new URL(route.request().url());
    urls.push(url);
    const frames = handler(url, urls.length);
    if (frames === null) return route.abort('failed');
    return route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: buildSseBody(frames)
    });
  });
  return urls;
}

const caption = (page: Page) => page.getByTestId('watch-caption-text');

test.describe('watch narrated (US-411)', () => {
  test('forwards tail and narrated params to the SSE URL; default stays plain', async ({ page }) => {
    const urls = await mockLive(page, (_u, n) => (n === 1 ? NARR_FRAMES.slice(0, 2) : null));
    await page.goto(`/watch/${NARR_ID}?tail=1&narrated=1`);
    await expect.poll(() => urls.length).toBeGreaterThan(0);
    expect(urls[0].searchParams.get('tail')).toBe('true');
    expect(urls[0].searchParams.get('speed')).toBe('100');
    expect(urls[0].searchParams.has('after')).toBe(false);
  });

  test('non-narrated mode unchanged: no params, no narration UI, immediate chat', async ({
    page
  }) => {
    const urls = await mockLive(page, (_u, n) => (n === 1 ? NARR_FRAMES.slice(0, 3) : null));
    await page.goto(`/watch/${NARR_ID}`);
    await expect(page.getByTestId('watch-chat-entry')).toHaveCount(2, { timeout: 15_000 });
    expect(urls[0].searchParams.has('tail')).toBe(false);
    expect(urls[0].searchParams.has('speed')).toBe(false);
    await expect(page.getByTestId('watch-narration')).toHaveCount(0);
    await expect(page.getByTestId('watch-start-narration')).toHaveCount(0);
  });

  test('audio blocks the next caption until ended; result waits for presentation', async ({
    page
  }) => {
    await stubAudio(page);
    await mockLive(page, (_u, n) => (n === 1 ? NARR_FRAMES : null));
    await page.goto(`/watch/${NARR_ID}?narrated=1`);
    // Nothing presented before the user gesture.
    await expect(page.getByTestId('watch-start-narration')).toBeVisible();
    await page.waitForTimeout(500);
    await expect(caption(page)).toHaveCount(0);
    await page.getByTestId('watch-start-narration').click();

    await expect(caption(page)).toHaveText('Day 1 begins.'); // phase cue first
    await expect
      .poll(() =>
        page.evaluate(() =>
          (window as unknown as { __audio: HTMLElement[] }).__audio[0].dataset.url
        )
      )
      .toMatch(/\/public\/games\/.+\/speech\/1\?cue=phase$/);
    await finishClip(page);
    await expect(caption(page)).toHaveText('First line.');
    await expect(page.getByTestId('watch-caption-speaker')).toHaveText(P1);
    await expect(
      page.locator(`[data-testid="watch-seat-row"][data-player-id="${P1}"]`)
    ).toHaveAttribute('data-speaking', 'true');
    // Long audio: nothing advances.
    await page.waitForTimeout(800);
    await expect(caption(page)).toHaveText('First line.');
    await expect(page.getByTestId('watch-chat-entry')).toHaveCount(1);

    await finishClip(page);
    await expect(caption(page)).toHaveText('Second line.');
    await finishClip(page);
    await expect(caption(page)).toHaveText(`${P2} has been eliminated.`);
    // Terminal frame received but not yet presented.
    await expect(page.getByTestId('watch-outcome-banner')).toHaveCount(0);
    await expect(page.getByTestId('watch-status')).not.toHaveText('Ended');
    await finishClip(page);
    await expect(caption(page)).toContainText('TOWN wins'); // no phase cue for END

    await expect(page.getByTestId('watch-outcome-banner')).toHaveCount(0);
    await finishClip(page);
    await expect(page.getByTestId('watch-outcome-banner')).toBeVisible();
    await expect(page.getByTestId('watch-winner')).toHaveText('TOWN');
  });

  test('audio error / 404 falls back to caption dwell and the queue continues', async ({ page }) => {
    await stubAudio(page);
    await mockLive(page, (_u, n) => (n === 1 ? NARR_FRAMES : null));
    await page.goto(`/watch/${NARR_ID}?narrated=1`);
    await page.getByTestId('watch-start-narration').click();
    await expect(caption(page)).toHaveText('Day 1 begins.');
    await finishClip(page, 'error'); // dwell ~1.4s then moves on
    await expect(caption(page)).toHaveText('First line.');
    await finishClip(page, 'error');
    await expect(caption(page)).toHaveText('Second line.', { timeout: 10_000 });
  });

  test('rejected play() falls back to dwell without losing frames', async ({ page }) => {
    await page.addInitScript(() => {
      HTMLMediaElement.prototype.play = () => Promise.reject(new Error('NotAllowedError'));
    });
    await mockLive(page, (_u, n) => (n === 1 ? NARR_FRAMES.slice(0, 3) : null));
    await page.goto(`/watch/${NARR_ID}?narrated=1`);
    await page.getByTestId('watch-start-narration').click();
    await expect(caption(page)).toHaveText('First line.', { timeout: 10_000 });
    await expect(caption(page)).toHaveText('Second line.', { timeout: 10_000 });
    await expect(page.getByTestId('watch-chat-entry')).toHaveCount(2);
  });

  test('mute keeps captions and dwell timing without audio', async ({ page }) => {
    await stubAudio(page);
    await mockLive(page, (_u, n) => (n === 1 ? NARR_FRAMES.slice(0, 3) : null));
    await page.goto(`/watch/${NARR_ID}?narrated=1`);
    await page.getByTestId('watch-mute').click();
    await page.getByTestId('watch-start-narration').click();
    await expect(caption(page)).toHaveText('Second line.', { timeout: 15_000 });
    const played = await page.evaluate(
      () => (window as unknown as { __audio: unknown[] }).__audio.length
    );
    expect(played).toBe(0);
  });

  test('reconnect resumes with after=<last> and shows no duplicate or lost lines', async ({
    page
  }) => {
    await stubAudio(page);
    const urls = await mockLive(page, (u, n) => {
      if (n === 1) return NARR_FRAMES.slice(0, 3);
      if (n === 2) {
        expect(u.searchParams.get('after')).toBe('3');
        return NARR_FRAMES.slice(2); // overlaps seq 3 on purpose
      }
      return null;
    });
    await page.goto(`/watch/${NARR_ID}?narrated=1`);
    await page.getByTestId('watch-start-narration').click();
    await expect.poll(() => urls.length, { timeout: 15_000 }).toBeGreaterThan(1);
    expect(urls[1].searchParams.get('after')).toBe('3');
    for (const line of ['Day 1 begins.', 'First line.', 'Second line.', `${P2} has been eliminated.`]) {
      await expect(caption(page)).toHaveText(line);
      await finishClip(page);
    }
    await expect(page.getByTestId('watch-chat-entry')).toHaveCount(2);
    await expect(caption(page)).toContainText('TOWN wins');
  });

  test('record layout hides chrome and shows a live-region caption', async ({ page }) => {
    await stubAudio(page);
    await mockLive(page, (_u, n) => (n === 1 ? NARR_FRAMES.slice(0, 3) : null));
    await page.goto(`/watch/${NARR_ID}?narrated=1&record=1`);
    await expect(page.locator('header')).toHaveCount(0);
    await expect(page.locator('footer')).toHaveCount(0);
    await page.getByTestId('watch-start-narration').click();
    await expect(caption(page)).toBeVisible();
    await expect(page.getByTestId('watch-caption')).toHaveAttribute('aria-live', 'polite');
    await expect(page.getByTestId('watch-shell')).toHaveAttribute('data-record', 'true');
  });

  test('chrome stays without record=1', async ({ page }) => {
    await mockLive(page, () => null);
    await page.goto(`/watch/${NARR_ID}`);
    await expect(page.locator('header')).toHaveCount(1);
  });
});
