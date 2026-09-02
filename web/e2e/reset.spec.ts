import { test, expect } from "./fixtures";

/**
 * The way back in (REQ-136, CR-028).
 *
 * The issuing half of the reset shipped without the redeeming half: the People
 * screen generated a code, and there was no form anywhere that would take one.
 * These two tests are the pair that could not drift apart again — the login page
 * has to offer the way through, and the screen it leads to has to actually spend
 * a code against the API.
 *
 * Deliberately not an end-to-end issue-then-redeem: succeeding would change the
 * password of the account the whole suite signs in with, and a test that has to
 * put the world back afterwards is a test that leaves it broken when it fails.
 * The half that is asserted here is the half that was missing.
 */
test.describe(() => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("the login page leads to somewhere a reset code can be spent", async ({ page }) => {
    await page.goto("/login");
    await page.getByRole("link", { name: /reset code/i }).click();
    await expect(page).toHaveURL(/\/reset$/);

    await expect(page.getByRole("heading", { name: /reset code/i })).toBeVisible();
    await expect(page.locator('input[autocomplete="username"]')).toBeVisible();
    await expect(page.locator('input[autocomplete="one-time-code"]')).toBeVisible();
    await expect(page.locator('input[autocomplete="new-password"]')).toHaveCount(2);
  });

  test("a code that was never issued is refused, and says nothing else", async ({ page }) => {
    await page.goto("/reset");
    // An address that cannot exist, so the one attempt this spends against the
    // throttle is spent on a key no other test uses.
    await page.fill('input[autocomplete="username"]', `nobody-${Date.now()}@example.test`);
    await page.fill('input[autocomplete="one-time-code"]', "ZZZZ-ZZZZ-ZZZZ");
    for (const field of await page.locator('input[autocomplete="new-password"]').all()) {
      await field.fill("correct-horse-battery-staple");
    }
    await page.click('button[type="submit"]');

    const alert = page.getByRole("alert");
    await expect(alert).toBeVisible();
    // Non-enumerable by design (ADR-008): the refusal must not distinguish "no
    // such account" from "no such code".
    await expect(alert).not.toContainText(/account/i);
    // And the credential stays out of the URL, where history and proxy logs are.
    await expect(page).toHaveURL(/\/reset$/);
  });
});
