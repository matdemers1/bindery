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

test("ticking 'into the vault' after the scan is kept, not dropped", async ({
  signedIn: page,
}) => {
  // The order that lost 309 files: scan first, read what was found, then tick
  // the box. Have a vault and have it open, the way a person would.
  await page.evaluate(async () => {
    const state = await fetch("/api/vault", { credentials: "same-origin" }).then((r) => r.json());
    if (!state.exists) {
      await fetch("/api/vault/setup", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passphrase: "an end to end vault passphrase", pin: "481516" }),
      });
    } else if (!state.unlocked) {
      await fetch("/api/vault/unlock", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pin: "481516" }),
      });
    }
  });
  await page.goto("/import");
  await page.getByRole("button", { name: /Scan the inbox/ }).click();
  await expect(page.getByText(/Scanned\. Nothing has been imported yet/)).toBeVisible();

  const box = page.getByRole("checkbox");
  await expect(box).toBeEnabled();
  // click + a retrying assertion, not check(): the box is a controlled input
  // and check() judges the state synchronously, before React has re-rendered.
  await box.click();
  await expect(box).toBeChecked();
  await expect(page.getByText(/This import now goes into the vault/)).toBeVisible();

  // Persisted on the import that is on screen, not held in the browser.
  const bound = await page.evaluate(async () => {
    const runs: { created_at: string; to_vault: boolean }[] = await fetch("/api/imports", {
      credentials: "same-origin",
    }).then((r) => r.json());
    // The list is not guaranteed newest-first; the one just scanned is.
    runs.sort((a, b) => b.created_at.localeCompare(a.created_at));
    return runs[0].to_vault;
  });
  expect(bound).toBe(true);
});
