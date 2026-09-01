import { test as base, expect, type Page } from "@playwright/test";

/**
 * Signing in, once per test.
 *
 * Credentials come from the environment rather than a constant: CI creates a
 * throwaway account with a generated password, and a password committed here
 * would be a password in the repository whatever we called it.
 */
// The account that owns the seeded corpus. `scripts/seed-demo.py` ingests its
// four invented documents into this user's library, so signing in as anyone
// else gives a correct but empty archive — and every retrieval test then fails
// for a reason that has nothing to do with retrieval.
export const EMAIL = process.env.BINDERY_EMAIL ?? "demo@example.com";
export const PASSWORD = process.env.BINDERY_PASSWORD ?? "";

export async function signIn(page: Page) {
  await page.goto("/login");
  await page.fill('input[autocomplete="username"]', EMAIL);
  await page.fill('input[autocomplete="current-password"]', PASSWORD);
  await page.click('button[type="submit"]');
  // Landing on Ask is the signal the session cookie survived. If the cookie is
  // dropped this hangs here rather than failing somewhere confusing later.
  await page.waitForURL("**/", { timeout: 20_000 });
  await expect(page.getByRole("navigation", { name: "Sections" })).toBeVisible();
}

export const test = base.extend<{ signedIn: Page }>({
  // Already signed in: `globalSetup` did it once and Playwright restores the
  // cookie. `signIn` remains exported for the tests that need a fresh, signed
  // -out context and say so.
  signedIn: async ({ page }, use) => {
    await page.goto("/");
    await expect(page.getByRole("navigation", { name: "Sections" })).toBeVisible();
    await use(page);
  },
});

export { expect };
