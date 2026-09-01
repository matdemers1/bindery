import { test, expect } from "./fixtures";

/**
 * The import screen (Phase 18, REQ-196).
 *
 * The inbox preset is the one thing that must always be there, and a scan of
 * it must produce a review rather than an import — nothing is read in until
 * somebody has seen what was found.
 */
test("the inbox can be scanned in one click and nothing is imported by it", async ({
  signedIn: page,
}) => {
  await page.goto("/import");
  const scan = page.getByRole("button", { name: /Scan the inbox/ });
  await expect(scan).toBeEnabled();
  await scan.click();

  await expect(page.getByText(/Scanned\. Nothing has been imported yet/)).toBeVisible();
  await expect(page.getByText("2 · What it found")).toBeVisible();
  await expect(page.getByRole("button", { name: /^Import all/ })).toBeVisible();
});

test("previous imports say what happened", async ({ signedIn: page }) => {
  // Make one rather than depend on another spec having run first — a test
  // that skips whenever it runs alone is a test that has stopped saying anything.
  await page.evaluate(async () => {
    const presets = await fetch("/api/imports/presets", { credentials: "same-origin" }).then((r) => r.json());
    const libraries = await fetch("/api/libraries", { credentials: "same-origin" }).then((r) => r.json());
    await fetch("/api/imports", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ library_id: libraries[0].id, root_path: presets.inbox }),
    });
  });
  await page.goto("/import");
  const rows = page.getByRole("button", { name: /Show details/ });
  await expect(rows.first()).toBeVisible();

  await rows.first().click();
  // `.first()`: the active run above the history renders the same detail, so
  // there are two of each of these on the page by design.
  await expect(page.getByRole("button", { name: /worker log/ }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: /failed/ }).first()).toBeVisible();
});

test("into the vault is not offered as a live choice while the vault is shut", async ({
  signedIn: page,
}) => {
  await page.evaluate(async () => {
    await fetch("/api/vault/lock", { method: "POST", credentials: "same-origin" });
  });
  await page.goto("/import");
  const box = page.getByRole("checkbox");
  await expect(box).toBeDisabled();
  await expect(page.getByText(/Unlock it to import into it|no vault yet/)).toBeVisible();
});
