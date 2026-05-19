#!/usr/bin/env node
// Single-command driver for `pnpm -C web/dashboard test:e2e` (US-070).
// Builds the static SPA with the e2e backend URL baked in, then hands off
// to Playwright. The Playwright config's globalSetup spawns the
// `padrino smoke localhost` harness; webServer runs `vite preview`.
//
// Pass-through args: `pnpm test:e2e -- --update-snapshots`, `-g leaderboard`,
// etc. propagate to the Playwright CLI.

import { spawn } from 'node:child_process';

const apiPort = process.env.PADRINO_E2E_API_PORT ?? '8123';
const dashboardPort = process.env.PADRINO_E2E_DASHBOARD_PORT ?? '5173';
const apiBaseUrl =
  process.env.PADRINO_E2E_API_BASE_URL ?? `http://127.0.0.1:${apiPort}`;

const env = {
  ...process.env,
  VITE_PADRINO_API_BASE_URL: apiBaseUrl,
  PADRINO_E2E_API_PORT: apiPort,
  PADRINO_E2E_DASHBOARD_PORT: dashboardPort,
  PADRINO_E2E_API_BASE_URL: apiBaseUrl
};

function run(cmd, args) {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, { stdio: 'inherit', env });
    child.on('exit', (code) => resolve(code ?? 1));
    child.on('error', () => resolve(1));
  });
}

const buildCode = await run('pnpm', ['exec', 'vite', 'build']);
if (buildCode !== 0) {
  console.error(`[e2e] vite build failed with code ${buildCode}`);
  process.exit(buildCode);
}

const playwrightCode = await run(
  'pnpm',
  ['exec', 'playwright', 'test', ...process.argv.slice(2)]
);
process.exit(playwrightCode);
