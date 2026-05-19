import { defineConfig, devices } from '@playwright/test';

const PORT = Number(process.env.PADRINO_E2E_DASHBOARD_PORT ?? '5173');
const API_PORT = Number(process.env.PADRINO_E2E_API_PORT ?? '8123');
const BASE_URL = process.env.PADRINO_E2E_BASE_URL ?? `http://127.0.0.1:${PORT}`;
const API_BASE_URL = process.env.PADRINO_E2E_API_BASE_URL ?? `http://127.0.0.1:${API_PORT}`;

export default defineConfig({
  testDir: './tests/e2e',
  testMatch: /.*\.spec\.ts$/,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  snapshotPathTemplate: 'tests/e2e/__snapshots__/{testFilePath}/{arg}-{projectName}{ext}',
  use: {
    baseURL: BASE_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
    actionTimeout: 15_000,
    navigationTimeout: 30_000
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] }
    }
  ],
  globalSetup: './tests/e2e/global-setup.ts',
  globalTeardown: './tests/e2e/global-teardown.ts',
  webServer: {
    command: `pnpm exec vite preview --port ${PORT} --host 127.0.0.1 --strictPort`,
    cwd: process.cwd(),
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    stdout: 'pipe',
    stderr: 'pipe',
    env: {
      ...process.env,
      VITE_PADRINO_API_BASE_URL: API_BASE_URL
    }
  }
});
