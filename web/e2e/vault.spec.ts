import type { Page } from "@playwright/test";

import { test, expect } from "./fixtures";

/**
 * The private vault (Phase 16).
 *
 * Driven through the real API rather than mocked, because the parts worth
 * checking are the ones that cross the boundary: that a locked vault renders
 * as locked, that a wrong PIN is refused and says how many attempts remain,
 * and that the screen never shows what is inside before it is opened.
 *
 * The seeded account may or may not already have a vault depending on what ran
 * before, so each test establishes the state it needs instead of assuming a
 * fresh one.
 */

const PASSPHRASE = "an end to end vault passphrase";
const PIN = "481516";

/** Whatever the account's vault is now, leave it existing and locked. */
async function lockedVault(page: Page) {
  const state = await page.evaluate(async () => {
    const r = await fetch("/api/vault", { credentials: "same-origin" });
    return r.json();
  });
  if (!state.exists) {
    await page.evaluate(
      async ([passphrase, pin]: [string, string]) => {
        await fetch("/api/vault/setup", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ passphrase, pin }),
        });
      },
      [PASSPHRASE, PIN] as [string, string],
    );
  }
  await page.evaluate(async () => {
    await fetch("/api/vault/lock", { method: "POST", credentials: "same-origin" });
  });
}

test("the vault is reachable from the sidebar", async ({ signedIn: page }) => {
  await page
    .getByRole("navigation", { name: "Sections" })
    .locator('a[href="/vault"]')
    .click();
  await expect(page.getByRole("heading", { name: "Vault" })).toBeVisible();
});

test("a locked vault says nothing about what is in it", async ({ signedIn: page }) => {
  await lockedVault(page);
  await page.goto("/vault");

  await expect(page.getByText("The vault is locked.")).toBeVisible();
  // No list, no count, no titles. A locked vault that reports how much it
  // holds has already said something about the contents.
  await expect(page.getByRole("button", { name: "Take out" })).toHaveCount(0);
  await expect(page.getByPlaceholder("Search inside the vault…")).toHaveCount(0);
});

test("a wrong PIN is refused and the attempts left are shown", async ({
  signedIn: page,
}) => {
  await lockedVault(page);
  await page.goto("/vault");

  await page.getByLabel("Vault PIN").fill("000000");
  await page.getByRole("button", { name: "Unlock" }).click();

  await expect(page.getByText("That PIN is wrong.")).toBeVisible();
  // How many tries remain is said once, under the field.
  await expect(page.getByText(/attempts? left\./)).toBeVisible();
  // Still shut.
  await expect(page.getByText("The vault is locked.")).toBeVisible();
});

test("the right PIN opens it, and Lock now shuts it again", async ({
  signedIn: page,
}) => {
  await lockedVault(page);
  await page.goto("/vault");

  await page.getByLabel("Vault PIN").fill(PIN);
  await page.getByRole("button", { name: "Unlock" }).click();

  await expect(page.getByText(/Open\. It locks itself/)).toBeVisible();
  await expect(page.getByPlaceholder("Search inside the vault…")).toBeVisible();

  await page.getByRole("button", { name: "Lock now" }).click();
  await expect(page.getByText("The vault is locked.")).toBeVisible();
});

test("search offers the vault but does not look in it unasked", async ({
  signedIn: page,
}) => {
  await lockedVault(page);
  await page.goto("/search?q=statement");

  const panel = page.getByRole("button", { name: /Also search the vault/ });
  await expect(panel).toBeVisible();
  // Nothing from the vault until it is asked for and unlocked.
  await expect(page.getByText("The vault is locked.")).toHaveCount(0);

  await panel.click();
  await expect(page.getByText(/The vault is locked/)).toBeVisible();
});

/** Open the seeded vault and return what is in it. */
async function unlockedVault(page: Page) {
  // Normalise to shut, then open it — rather than testing `count()` on a form
  // that may not have rendered yet. `count()` does not auto-wait, so the
  // conditional silently skips and the unlock never happens.
  await lockedVault(page);
  await page.goto("/vault");
  await page.getByLabel("Vault PIN").fill(PIN);
  await page.getByRole("button", { name: "Unlock" }).click();
  await expect(page.getByText(/Open\. It locks itself/)).toBeVisible();

  return await page.evaluate(async () => {
    const r = await fetch("/api/vault/items", { credentials: "same-origin" });
    return (await r.json()) as {
      document_id: string;
      title: string | null;
      is_image: boolean;
    }[];
  });
}

test("the vault separates documents from photos", async ({ signedIn: page }) => {
  const items = await unlockedVault(page);

  // No `test.skip`. `scripts/seed-e2e-states.py` seals one document and one
  // photograph into this account's vault, so an empty vault is a broken fixture
  // rather than a reason for this test to say nothing — which is what it did on
  // every run for two phases.
  expect(items.length, "the vault fixture is empty; check seed-e2e-states.py")
    .toBeGreaterThan(0);
  expect(items.some((item) => item.is_image)).toBe(true);
  expect(items.some((item) => !item.is_image)).toBe(true);

  // Tabs, not buttons: they carry `role="tab"`, which `getByRole("button")`
  // does not match — a mismatch the permanent skip above was hiding.
  // Case-insensitive because the labels are capitalised by CSS, which does not
  // change the accessible name.
  await expect(page.getByRole("tab", { name: /documents/i })).toBeVisible();
  await expect(page.getByRole("tab", { name: /photos/i })).toBeVisible();

  await page.getByRole("tab", { name: /photos/i }).click();
  // Rendered, not listed: an <img> whose source is the decrypted original.
  await expect(page.locator('img[src*="/api/vault/items/"]').first()).toBeVisible();
});

test("take out puts a sealed document back into the archive", async ({
  signedIn: page,
}) => {
  // The move *out* of the vault, driven the way a person drives it. ADR-012
  // grants the vault the only exemption from REQ-090 — it is the one path
  // allowed to delete an original — and until now the browser had never been
  // near either direction of it.
  const before = await unlockedVault(page);
  const document = before.find((item) => !item.is_image);
  expect(document, "no sealed document to take out").toBeTruthy();
  const title = document!.title ?? "";
  expect(title.length).toBeGreaterThan(0);

  await page.getByRole("tab", { name: /documents/i }).click();
  const row = page.locator("li").filter({ hasText: title });
  await expect(row).toHaveCount(1);
  await row.getByRole("button", { name: "Take out" }).click();

  // Gone from the vault…
  await expect(row).toHaveCount(0);

  // …and back in the archive, decrypted and findable again.
  const restored = await page.evaluate(async (id) => {
    const r = await fetch(`/api/documents/${id}`, { credentials: "same-origin" });
    return { status: r.status, body: r.status === 200 ? await r.json() : null };
  }, document!.document_id);
  expect(restored.status).toBe(200);
  expect(restored.body.document.title).toBe(title);

  // Put it back, so this spec leaves the vault the way it found it and the
  // fixture survives a re-run.
  await page.evaluate(async (id) => {
    await fetch(`/api/vault/items/${id}`, {
      method: "POST",
      credentials: "same-origin",
    });
  }, document!.document_id);
});
