import { test, expect } from "./fixtures";

/**
 * Declined work, and acknowledging what genuinely failed (ADR-011).
 *
 * The bug these guard is the one that prompted Phase 14: a badge that was
 * always on, lit by three files the pipeline had correctly refused. A warning
 * that cannot be cleared is one people learn to ignore, and that is not
 * something a unit test can observe — it lives in the sidebar.
 */
test("a refusal is listed but offers nothing to acknowledge", async ({ signedIn: page }) => {
  await page.goto("/pipeline");
  const refused = page.locator("section").filter({
    has: page.getByRole("heading", { name: "Refused" }),
  });
  // Not a skip. `scripts/seed-e2e-states.py` puts a declined job in this
  // archive precisely so this runs; if it is missing, the fixture is broken
  // and a skip would report that as a pass.
  await expect(refused, "seed-e2e-states.py did not run").toHaveCount(1);
  // Listed, so "why did that file never appear?" stays answerable...
  await expect(refused.first()).toBeVisible();
  // ...and not actionable, because there is nothing to fix.
  await expect(refused.first().getByRole("button", { name: "Acknowledge" })).toHaveCount(0);
});

test("acknowledging a dead letter clears the badge without hiding the job", async ({
  signedIn: page,
}) => {
  await page.goto("/pipeline");
  const gaveUp = page.locator("section").filter({
    has: page.getByRole("heading", { name: "Gave up" }),
  });
  await expect(gaveUp, "seed-e2e-states.py did not run").toHaveCount(1);

  const before = await gaveUp.first().getByRole("button", { name: "Acknowledge" }).count();
  expect(before, "the seeded dead letter is already acknowledged").toBeGreaterThan(0);

  const rowsBefore = await gaveUp.first().locator("li").count();
  for (let i = 0; i < before; i += 1) {
    await gaveUp.first().getByRole("button", { name: "Acknowledge" }).first().click();
    await page.waitForTimeout(800);
  }

  // The row survives, with its error. Acknowledging is the weakest possible
  // action: it does not retry, hide or delete (REQ-169).
  await expect(gaveUp.first().locator("li")).toHaveCount(rowsBefore);
  await expect(gaveUp.first().getByRole("button", { name: "Undo" }).first()).toBeVisible();

  // And the badge it was lighting is gone.
  const nav = page.getByRole("navigation", { name: "Sections" });
  await expect(nav.getByRole("link", { name: "Trust" }).getByText("!")).toHaveCount(0);
});
