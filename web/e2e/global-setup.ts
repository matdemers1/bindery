import { chromium, type FullConfig } from "@playwright/test";

import { EMAIL, PASSWORD } from "./fixtures";

/**
 * Sign in once, for the whole run.
 *
 * Not an optimisation. Signing in per test meant eleven attempts from one
 * address in a few seconds, and `api/auth/throttle.py` throttles exactly that:
 * four free attempts, then exponential backoff. The login page is the only
 * thing between the open internet and the archive (invariant 9), so the
 * throttle is load-bearing and the suite is what should adapt.
 *
 * The symptom was worth recording, because it did not look like rate limiting:
 * a *different* test failed on each run, always at `waitForURL` after a
 * successful-looking submit, which reads like flakiness in whatever test drew
 * the short straw.
 */
export default async function globalSetup(config: FullConfig) {
  if (!PASSWORD) {
    throw new Error(
      "BINDERY_PASSWORD is unset — the suite cannot sign in. CI generates one " +
        "per run; locally, create an account and export it.",
    );
  }
  const baseURL = config.projects[0]?.use?.baseURL ?? "http://localhost";
  const browser = await chromium.launch();
  const page = await browser.newPage({ baseURL });

  await page.goto("/login");
  await page.fill('input[autocomplete="username"]', EMAIL);
  await page.fill('input[autocomplete="current-password"]', PASSWORD);
  await page.click('button[type="submit"]');
  await page.waitForURL("**/", { timeout: 30_000 });

  await page.context().storageState({ path: "e2e/.auth/state.json" });
  await browser.close();
}
