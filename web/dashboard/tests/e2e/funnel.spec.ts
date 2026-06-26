import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { expect, test, type Page } from '@playwright/test';

const HOW_TO_PLAY_DISMISSED_KEY = 'padrino:how-to-play-dismissed:v1';
const API_BASE_URL = (
  process.env.PADRINO_E2E_API_BASE_URL ??
  `http://127.0.0.1:${process.env.PADRINO_E2E_API_PORT ?? '8123'}`
).replace(/\/+$/, '');
const REPO_ROOT = process.env.PADRINO_REPO_ROOT ?? join(process.cwd(), '..', '..');

interface SetupState {
  dbPath?: string;
  skipBackend?: boolean;
}

interface GameSnapshot {
  status: string | null;
  completed_at: string | null;
  terminal_result: Record<string, unknown> | null;
  scientific_rating_events: number;
  human_stats_games: number;
  human_guess_count: number;
}

function readSetupState(): SetupState {
  const stateFile = process.env.PADRINO_E2E_STATE_FILE;
  if (!stateFile || !existsSync(stateFile)) {
    throw new Error('PADRINO_E2E_STATE_FILE was not written by globalSetup');
  }
  return JSON.parse(readFileSync(stateFile, 'utf-8')) as SetupState;
}

function queryGameSnapshot(gameId: string): GameSnapshot {
  const state = readSetupState();
  if (!state.dbPath) {
    throw new Error('US-287 funnel requires the globalSetup SQLite database');
  }

  const script = String.raw`
import json
import sqlite3
import sys
import uuid

db_path = sys.argv[1]
needle = uuid.UUID(sys.argv[2])

def norm(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        return uuid.UUID(bytes=value)
    return uuid.UUID(str(value))

conn = sqlite3.connect(db_path, timeout=5.0)
conn.row_factory = sqlite3.Row
try:
    game_row = None
    for row in conn.execute("select id, status, completed_at, terminal_result from games"):
        if norm(row["id"]) == needle:
            game_row = row
            break

    scientific_rating_events = 0
    for row in conn.execute(
        "select re.game_id as game_id, l.kind as league_kind "
        "from rating_events re join leagues l on l.id = re.league_id"
    ):
        if norm(row["game_id"]) == needle and row["league_kind"] == "SCIENTIFIC":
            scientific_rating_events += 1

    human_principals = set()
    for row in conn.execute(
        "select game_id, occupant_principal_id, seat_kind from game_seats "
        "where occupant_principal_id is not null"
    ):
        if norm(row["game_id"]) == needle and row["seat_kind"] in ("HUMAN", "AI_TAKEOVER"):
            human_principals.add(norm(row["occupant_principal_id"]))

    human_stats_games = 0
    for row in conn.execute("select principal_id, games from human_player_stats"):
        if norm(row["principal_id"]) in human_principals:
            human_stats_games += int(row["games"])

    human_guess_count = 0
    for row in conn.execute("select game_id from human_turing_guesses"):
        if norm(row["game_id"]) == needle:
            human_guess_count += 1

    terminal_result = None
    if game_row is not None and game_row["terminal_result"] is not None:
        terminal_result = json.loads(game_row["terminal_result"])
    print(json.dumps({
        "status": None if game_row is None else game_row["status"],
        "completed_at": None if game_row is None else game_row["completed_at"],
        "terminal_result": terminal_result,
        "scientific_rating_events": scientific_rating_events,
        "human_stats_games": human_stats_games,
        "human_guess_count": human_guess_count,
    }))
finally:
    conn.close()
`;

  const output = execFileSync('uv', ['run', 'python', '-c', script, state.dbPath, gameId], {
    cwd: REPO_ROOT,
    encoding: 'utf-8'
  });
  return JSON.parse(output) as GameSnapshot;
}

async function driveOneLegalAction(page: Page, gameId: string, attempt: number): Promise<string> {
  const observationResponse = await page.request.get(
    `${API_BASE_URL}/human/games/${encodeURIComponent(gameId)}/observation/stream?attempt=${attempt}`,
    { timeout: 15_000 }
  );
  if (!observationResponse.ok()) return `observation:${observationResponse.status()}`;

  const body = await observationResponse.text();
  const frames = body
    .split(/\n\n+/)
    .flatMap((block) =>
      block
        .split('\n')
        .filter((line) => line.startsWith('data:'))
        .map((line) => JSON.parse(line.slice(5).trim()) as Record<string, unknown>)
    );
  const observation = frames.find((frame) => frame['type'] === 'observation') as
    | {
        legal_actions?: {
          allowed_action_types?: string[];
          legal_targets?: string[];
        };
      }
    | undefined;
  const legal = observation?.legal_actions;
  const allowed = legal?.allowed_action_types ?? [];
  const targets = legal?.legal_targets ?? [];
  const safeAction = allowed.find((type) => type === 'ABSTAIN' || type === 'NOOP');
  let action: { type: string; target: string | null } | null = null;
  if (safeAction) {
    action = { type: safeAction, target: null };
  } else if (targets.length > 0 && allowed.length > 0) {
    action = { type: allowed[0], target: targets[0] };
  }
  if (action === null) return 'no-action';

  const response = await page.request.post(
    `${API_BASE_URL}/human/games/${encodeURIComponent(gameId)}/actions`,
    {
      data: { action, idempotency_key: `us287-${attempt}` },
      timeout: 15_000
    }
  );
  return response.ok() ? 'accepted' : `action:${response.status()}`;
}

async function driveUntilTerminal(
  page: Page,
  gameId: string
): Promise<{ snapshot: GameSnapshot; actionAccepted: boolean; sawServerError: boolean }> {
  const deadline = Date.now() + 120_000;
  let attempt = 0;
  let actionAccepted = false;
  let sawServerError = false;
  let last: GameSnapshot = queryGameSnapshot(gameId);

  while (Date.now() < deadline) {
    last = queryGameSnapshot(gameId);
    if (last.status === 'COMPLETED') {
      return { snapshot: last, actionAccepted, sawServerError };
    }
    const result = await driveOneLegalAction(page, gameId, attempt);
    actionAccepted ||= result === 'accepted';
    if (result.startsWith('action:5')) sawServerError = true;
    attempt += 1;
    await page.waitForTimeout(250);
  }

  throw new Error(`game ${gameId} did not complete, last snapshot: ${JSON.stringify(last)}`);
}

test.describe('US-287 cold visitor funnel', () => {
  test('cold visitor plays a real human-lane match through reveal and profile stats', async ({
    page
  }) => {
    await page.addInitScript((key) => window.localStorage.removeItem(key), HOW_TO_PLAY_DISMISSED_KEY);

    const matchResponsePromise = page.waitForResponse(
      (response) =>
        response.url().endsWith('/human/match') &&
        response.request().method() === 'POST' &&
        response.status() === 201,
      { timeout: 45_000 }
    );

    await page.goto('/');
    await expect(page.getByTestId('home-play-vs-ai-cta')).toBeVisible();
    await page.getByTestId('home-play-vs-ai-cta').click();
    await expect(page.getByTestId('how-to-play-modal')).toBeVisible();
    await page.getByTestId('how-to-play-continue').click();
    await expect(page.getByTestId('home-consent-row')).toBeVisible({ timeout: 15_000 });
    await page.getByTestId('home-consent-accept').click();
    await expect(page.getByTestId('match-queue-screen')).toBeVisible({ timeout: 15_000 });

    const matchResponse = await matchResponsePromise;
    const matchPayload = (await matchResponse.json()) as { game_id: string };
    const gameId = matchPayload.game_id;
    await expect(page).toHaveURL(new RegExp(`/play/${gameId}$`), { timeout: 30_000 });
    await expect(page.getByTestId('play-title')).toBeVisible();

    const terminal = await driveUntilTerminal(page, gameId);
    expect(terminal.snapshot.status).toBe('COMPLETED');
    expect(terminal.snapshot.completed_at).toBeTruthy();
    expect(terminal.snapshot.terminal_result).not.toBeNull();
    expect(terminal.snapshot.scientific_rating_events).toBe(0);
    expect(terminal.snapshot.human_stats_games).toBeGreaterThanOrEqual(1);
    // The human action-submission path (authz, consent, phase/legal-action
    // validation, rate limiting, atomic persistence) is covered in depth by
    // tests/api/test_human_action_channel.py + test_human_rate_limit_peek_commit.py.
    // Under the fast e2e phase deadline most driven actions are coerced as
    // out-of-phase (4xx, expected); the funnel only guards that the action
    // channel never 500s while a real game is driven to terminal.
    expect(terminal.sawServerError).toBe(false);

    await page.goto(`/play/${gameId}/reveal`);
    await expect(page.getByTestId('reveal-title')).toBeVisible();
    await expect(page.getByTestId('guess-panel')).toBeVisible({ timeout: 15_000 });
    // Click AI on every guessable row. The reveal renders one row per seat
    // EXCEPT the viewer's own, gated on the viewer's seat resolving, so the AI
    // button count is stable; click the buttons directly (not row indices) so
    // the loop never targets a row without an AI button.
    const guessAiButtons = page.getByTestId('guess-seat-row').getByRole('button', { name: 'AI' });
    await expect.poll(() => guessAiButtons.count(), { timeout: 15_000 }).toBeGreaterThan(0);
    const guessAiCount = await guessAiButtons.count();
    for (let index = 0; index < guessAiCount; index += 1) {
      await guessAiButtons.nth(index).click();
    }
    await page.getByTestId('guess-submit').click();
    await expect(page.getByTestId('reveal-accuracy')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('reveal-board')).toBeVisible({ timeout: 15_000 });
    await expect.poll(() => page.getByTestId('reveal-seat-row').count()).toBeGreaterThan(0);

    const afterGuess = queryGameSnapshot(gameId);
    expect(afterGuess.scientific_rating_events).toBe(0);
    expect(afterGuess.human_guess_count).toBe(1);

    await page.goto('/profile');
    await expect(page.getByTestId('profile-signed-out')).toHaveCount(0);
    await expect(page.getByTestId('profile-guest-upsell')).toContainText('Sign in to save');
    await expect(page.getByTestId('profile-stats')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('profile-games')).toHaveText(/^[1-9][0-9]*$/);
    await expect(page.getByTestId('profile-history')).toBeVisible({ timeout: 15_000 });
    const historyLink = page.locator(`[data-testid="profile-history-link"][href="/play/${gameId}/reveal"]`);
    await expect(historyLink).toBeVisible();
  });
});
