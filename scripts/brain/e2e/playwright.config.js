// Playwright E2E for the Shared Brain dashboard (UI + JSON API).
//
// The dashboard is a stdlib http.server in scripts/brain/dashboard.py. Playwright
// starts it on a dedicated port so a suite run never collides with a dashboard the
// operator already has open on the default 3333.
const { defineConfig, devices } = require('@playwright/test');

const PORT = Number(process.env.BRAIN_E2E_PORT || 3399);

module.exports = defineConfig({
  testDir: './tests',
  // The brain store is shared mutable state on disk, so tests run serially.
  // Parallel workers would race each other's notes and swarm task files.
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]],
  timeout: 60_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: `python3 ../dashboard.py --port ${PORT} --host 127.0.0.1`,
    url: `http://127.0.0.1:${PORT}/api/status`,
    reuseExistingServer: !process.env.CI,
    // The dashboard also spawns a semantic-search sidecar on first boot, which can
    // take a while on a cold cache; the API is served before that completes.
    timeout: 120_000,
    stdout: 'pipe',
    stderr: 'pipe',
  },
});
