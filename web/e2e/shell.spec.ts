import { test, expect } from "./fixtures";

/**
 * The shell, which every other screen is reached through.
 *
 * Phase 14 moved five destinations into the account menu. The Python guard
 * checks the route table against the link table; this checks that the links
 * a person can actually see and click are there.
 */
test("the sidebar shows the daily destinations and hides setup", async ({ signedIn: page }) => {
  const nav = page.getByRole("navigation", { name: "Sections" });
  // By href rather than by accessible name. A badge is part of the link's
  // name — Trust reads as "Trust !" while something needs attention — so an
  // exact-name match passes or fails on whether the archive happens to be
  // healthy, which is not what this test is about.
  for (const href of ["/", "/search", "/archive", "/files", "/photos",
                      "/review", "/organise", "/import", "/rules",
                      "/trust", "/pipeline"]) {
    await expect(nav.locator(`a[href="${href}"]`), `${href} is missing`).toBeVisible();
  }
  // Set up once and then forgotten: reachable, not occupying the daily list.
  await expect(nav.locator('a[href="/settings"]')).toHaveCount(0);
});

test("the account menu reveals the setup screens", async ({ signedIn: page }) => {
  await page.getByRole("button", { name: "Account and setup" }).click();
  for (const href of ["/account", "/libraries", "/settings", "/help"]) {
    await expect(page.locator(`aside a[href="${href}"]`), `${href} is missing`).toBeVisible();
  }
});

test("every visible destination actually loads", async ({ signedIn: page }) => {
  // A sidebar that links to a broken screen is worse than one that omits it.
  for (const path of ["/search", "/archive", "/files", "/photos", "/review",
                      "/organise", "/import", "/rules", "/trust", "/pipeline"]) {
    const response = await page.goto(path);
    expect(response?.status(), `${path} did not load`).toBeLessThan(400);
    await expect(page.getByRole("navigation", { name: "Sections" })).toBeVisible();
  }
});

// A deliberately signed-out context: the stored session would skip the login
// page entirely. One failed attempt is well inside the throttle's free budget.
test.describe(() => {
  test.use({ storageState: { cookies: [], origins: [] } });
  test("a wrong password is refused without saying which half was wrong", async ({ page }) => {
  await page.goto("/login");
  await page.fill('input[autocomplete="username"]', "nobody@example.com");
  await page.fill('input[autocomplete="current-password"]', "definitely-not-the-password");
  await page.click('button[type="submit"]');
  // Non-enumerable by design: the message must not distinguish "no such
  // account" from "wrong password" (ADR-008).
  await expect(page.getByText(/were not accepted/i)).toBeVisible();
  await expect(page).toHaveURL(/login/);
  });
});
