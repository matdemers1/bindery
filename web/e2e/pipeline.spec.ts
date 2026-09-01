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
  // Until none remain, rather than a fixed count. The badge reflects *all*
  // outstanding work, so asserting it goes out after acknowledging a fixed
  // number is a global claim made from a local action — and the suite's own
  // upload spec adds a document whose classification dead-letters without an
  // API key, so "how many" is not something this test can know in advance.
  for (let guard = 0; guard < 25; guard += 1) {
    const remaining = gaveUp.first().getByRole("button", { name: "Acknowledge" });
    if ((await remaining.count()) === 0) break;
    await remaining.first().click();
    await page.waitForTimeout(600);
  }
  await expect(
    gaveUp.first().getByRole("button", { name: "Acknowledge" }),
    "something is still outstanding, so the badge assertion below cannot mean anything",
  ).toHaveCount(0);

  // The row survives, with its error. Acknowledging is the weakest possible
  // action: it does not retry, hide or delete (REQ-169).
  await expect(gaveUp.first().locator("li")).toHaveCount(rowsBefore);
  await expect(gaveUp.first().getByRole("button", { name: "Undo" }).first()).toBeVisible();

  // And the badge it was lighting is gone.
  const nav = page.getByRole("navigation", { name: "Sections" });
  await expect(nav.getByRole("link", { name: "Trust" }).getByText("!")).toHaveCount(0);
});
