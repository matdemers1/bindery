import { test, expect } from "./fixtures";

/**
 * Correcting a document (Phase 17).
 *
 * Driven through the real API, because the properties worth checking are the
 * ones that cross the boundary: that a correction is written, that it is
 * marked as yours rather than the model's, and that undo puts it back.
 */

test("a document's title can be corrected from the viewer", async ({
  signedIn: page,
}) => {
  const first = await page.evaluate(async () => {
    const r = await fetch("/api/documents?limit=1", { credentials: "same-origin" });
    const body = await r.json();
    return Array.isArray(body) ? body[0] : body.documents?.[0];
  });
  test.skip(!first, "the seeded corpus has no documents");

  await page.goto(`/document/${first.id}/page/1`);
  await page.getByRole("button", { name: "Edit" }).click();

  const title = page.getByPlaceholder("Untitled");
  await expect(title).toBeVisible();
  await title.fill("Corrected by the e2e run");
  await page.getByRole("button", { name: "Save" }).click();

  await expect(page.getByText(/title updated/)).toBeVisible();
  // And it is marked as a person's doing, not the model's — REQ-064.
  await page.getByRole("button", { name: "Edit" }).click();
  await expect(page.getByText("you set this").first()).toBeVisible();
});

test("an edit can be undone", async ({ signedIn: page }) => {
  const first = await page.evaluate(async () => {
    const r = await fetch("/api/documents?limit=1", { credentials: "same-origin" });
    const body = await r.json();
    return Array.isArray(body) ? body[0] : body.documents?.[0];
  });
  test.skip(!first, "the seeded corpus has no documents");

  await page.goto(`/document/${first.id}/page/1`);
  await page.getByRole("button", { name: "Edit" }).click();
  await page.getByPlaceholder("Untitled").fill("A title to walk back");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText(/updated/)).toBeVisible();

  await page.getByRole("button", { name: "Edit" }).click();
  await page.getByRole("button", { name: "Undo last change" }).click();
  await expect(page.getByText("Reverted.")).toBeVisible();
});

test("the review queue offers correcting before accepting", async ({
  signedIn: page,
}) => {
  await page.goto("/review");
  // `count()` does not auto-wait, so asking before React has rendered always
  // answers zero — and the test then skips itself for the wrong reason.
  await page.waitForLoadState("networkidle");
  const correct = page.getByRole("button", { name: "Correct" });
  // The queue is empty when everything filed itself, which is a legitimate
  // state rather than a failure. `scripts/seed-e2e-states.py` puts one
  // document here so this does not silently become a test that never runs.
  test.skip((await correct.count()) === 0, "nothing is waiting for review");

  await correct.click();
  await expect(page.getByPlaceholder("Untitled")).toBeVisible();
  // Accept is still there: correcting is a step before it, not instead of it.
  await expect(page.getByRole("button", { name: "Accept" })).toBeVisible();
});
