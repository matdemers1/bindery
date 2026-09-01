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
