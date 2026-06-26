/**
 * Playwright globalSetup for the Padrino dashboard e2e suite (US-070).
 *
 * Spawns `padrino smoke localhost --with-human-lane --keep-running --port <api-port>` to:
 *   - bootstrap a fresh SQLite database
 *   - bring up the API + scheduler + human-lane worker as detached child processes
 *   - drive one mock-adapter gauntlet to completion
 *   - export + ingest one game so the public read endpoints have data
 *
 * The smoke parent process exits after the flow; the API + scheduler
 * children remain alive because of `--keep-running`. They are spawned in
 * a fresh process group (detached + setsid via `spawn` semantics) so
 * `globalTeardown` can SIGTERM the entire group cleanly.
 *
 * Environment overrides:
 *   - PADRINO_E2E_API_PORT      (default 8123)
 *   - PADRINO_E2E_DB_PATH       (default ./.padrino-e2e.db inside cwd)
 *   - PADRINO_E2E_SMOKE_TIMEOUT (default 360 seconds)
 *   - PADRINO_E2E_SKIP_BACKEND  (set to "1" to assume backend is already up)
 */

import { spawn } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { FullConfig } from '@playwright/test';

const STATE_FILE = join(tmpdir(), `padrino-e2e-state-${process.pid}.json`);

async function waitForHealth(url: string, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  let lastError: unknown = null;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) return;
      lastError = `status ${response.status}`;
    } catch (e) {
      lastError = e;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(
    `backend did not become healthy at ${url} within ${timeoutMs}ms (last error: ${String(lastError)})`
  );
}

async function spawnSmoke(apiPort: number, dbPath: string, timeoutMs: number): Promise<number> {
  return await new Promise<number>((resolve, reject) => {
    const args = [
      'run',
      'padrino',
      'smoke',
      'localhost',
      '--keep-running',
      '--with-human-lane',
      '--port',
      String(apiPort),
      '--db-url',
      `sqlite+aiosqlite:///${dbPath}`,
      '--timeout-s',
      '120'
    ];
    const corsOrigin =
      process.env.PADRINO_E2E_DASHBOARD_ORIGIN ??
      `http://127.0.0.1:${process.env.PADRINO_E2E_DASHBOARD_PORT ?? '5173'}`;
    const child = spawn('uv', args, {
      cwd: process.env.PADRINO_REPO_ROOT ?? join(process.cwd(), '..', '..'),
      detached: true,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: {
        ...process.env,
        PADRINO_CORS_ALLOW_ORIGINS: corsOrigin,
        PADRINO_HUMAN_PHASE_DEADLINE_SECONDS:
          process.env.PADRINO_HUMAN_PHASE_DEADLINE_SECONDS ?? '0.05',
        PADRINO_HUMAN_RELEASE_DELAY_SECONDS:
          process.env.PADRINO_HUMAN_RELEASE_DELAY_SECONDS ?? '0',
        // The public spectator site reads the /public/* surface anonymously
        // (the production public-surface-only deployment serves these without a
        // key). Enable anonymous public reads so the e2e suite exercises the
        // real public endpoints the dashboard now depends on.
        PADRINO_PUBLIC_LEADERBOARD_ANONYMOUS: 'true'
      }
    });
    const stdoutChunks: string[] = [];
    const stderrChunks: string[] = [];
    child.stdout?.on('data', (b: Buffer) => stdoutChunks.push(b.toString()));
    child.stderr?.on('data', (b: Buffer) => stderrChunks.push(b.toString()));

    const timer = setTimeout(() => {
      try {
        // negative pid signals the whole process group
        if (child.pid) process.kill(-child.pid, 'SIGTERM');
      } catch {
        // ignore
      }
      reject(
        new Error(
          `padrino smoke localhost timed out after ${timeoutMs}ms\n` +
            `stdout:\n${stdoutChunks.join('')}\nstderr:\n${stderrChunks.join('')}`
        )
      );
    }, timeoutMs);

    child.on('exit', (code) => {
      clearTimeout(timer);
      if (code !== 0) {
        reject(
          new Error(
            `padrino smoke localhost exited with code ${code}\n` +
              `stdout:\n${stdoutChunks.join('')}\nstderr:\n${stderrChunks.join('')}`
          )
        );
        return;
      }
      if (child.pid === undefined) {
        reject(new Error('padrino smoke localhost: child.pid was undefined'));
        return;
      }
      resolve(child.pid);
    });
    child.on('error', (err) => {
      clearTimeout(timer);
      reject(err);
    });
  });
}

export default async function globalSetup(_config: FullConfig): Promise<void> {
  if (process.env.PADRINO_E2E_SKIP_BACKEND === '1') {
    const apiPort = Number(process.env.PADRINO_E2E_API_PORT ?? '8123');
    await waitForHealth(`http://127.0.0.1:${apiPort}/healthz`, 30_000);
    writeFileSync(STATE_FILE, JSON.stringify({ skipBackend: true }), 'utf-8');
    process.env.PADRINO_E2E_STATE_FILE = STATE_FILE;
    return;
  }

  const apiPort = Number(process.env.PADRINO_E2E_API_PORT ?? '8123');
  // 360s default: the smoke runs the full migration chain + a demo gauntlet
  // before the keep-running server serves /health, which comfortably fits in
  // <60s locally but can exceed the old 180s budget on slow CI runners.
  const timeoutMs = Number(process.env.PADRINO_E2E_SMOKE_TIMEOUT ?? '360') * 1000;
  const sandbox = mkdtempSync(join(tmpdir(), 'padrino-e2e-'));
  const dbPath = process.env.PADRINO_E2E_DB_PATH ?? join(sandbox, 'padrino-e2e.db');

  const smokePid = await spawnSmoke(apiPort, dbPath, timeoutMs);
  await waitForHealth(`http://127.0.0.1:${apiPort}/healthz`, 30_000);

  writeFileSync(
    STATE_FILE,
    JSON.stringify({ smokePid, sandbox, dbPath, apiPort }),
    'utf-8'
  );
  process.env.PADRINO_E2E_STATE_FILE = STATE_FILE;
}
