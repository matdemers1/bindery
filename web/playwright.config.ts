import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end tests against the real stack.
 *
 * `BINDERY_URL` defaults to `http://localhost` because these run *inside* the
 * compose network, sharing the web container's namespace. That is not a
 * convenience: there are no published host ports (REQ-104), and the session
 * cookie is `Secure` — a browser reached over plain `http://web` discards it
 * silently, the login succeeds, every later request 401s, and the screen sits
 * on the login form looking like a wrong password. `localhost` is the one
 * origin Chrome treats as trustworthy without TLS.
 */
export default defineConfig({
  testDir: "./e2e",
  // One sign-in for the whole run, reused as stored state. Per-test sign-ins
  // trip the app's own login throttle — see e2e/global-setup.ts.
  globalSetup: "./e2e/global-setup.ts",
  // The archive is the shared fixture — one sign-in, one seeded corpus — so
  // parallel workers would race each other's state. These are minutes, not
  // hours, and a flaky suite is worth less than a slow one.
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  // No retries. A test that passes on the second attempt is a test that has
  // stopped telling you anything, and this suite exists to gate a deploy.
  retries: 0,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? [["github"], ["list"]] : [["list"]],
  use: {
    baseURL: process.env.BINDERY_URL ?? "http://localhost",
    storageState: "e2e/.auth/state.json",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
