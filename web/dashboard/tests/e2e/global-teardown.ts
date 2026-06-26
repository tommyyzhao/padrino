/**
 * Playwright globalTeardown for the dashboard e2e suite (US-070).
 *
 * Reads the state file written by globalSetup and SIGTERMs the smoke
 * harness's process group, which kills the detached API + scheduler
 * children spawned by `padrino smoke localhost --with-human-lane --keep-running`. A second
 * SIGKILL follows if anything is still alive after 5 seconds.
 */

import { existsSync, readFileSync, rmSync } from 'node:fs';

interface SetupState {
  smokePid?: number;
  sandbox?: string;
  skipBackend?: boolean;
}

function killGroup(pid: number, signal: NodeJS.Signals): void {
  try {
    process.kill(-pid, signal);
  } catch {
    // ignore — group may already be gone
  }
}

export default async function globalTeardown(): Promise<void> {
  const stateFile = process.env.PADRINO_E2E_STATE_FILE;
  if (!stateFile || !existsSync(stateFile)) return;

  const state = JSON.parse(readFileSync(stateFile, 'utf-8')) as SetupState;
  rmSync(stateFile, { force: true });

  if (state.skipBackend) return;

  if (typeof state.smokePid === 'number') {
    killGroup(state.smokePid, 'SIGTERM');
    await new Promise((resolve) => setTimeout(resolve, 5_000));
    killGroup(state.smokePid, 'SIGKILL');
  }

  if (state.sandbox) {
    rmSync(state.sandbox, { recursive: true, force: true });
  }
}
