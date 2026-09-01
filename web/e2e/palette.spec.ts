import { test, expect } from "./fixtures";

/**
 * ⌘K → type → Enter → the viewer opens on the page.
 *
 * `CommandPalette.tsx` calls that path "the product", and until now nothing
 * tested it. Added alongside the refactor that stopped the palette clearing
 * itself in an effect and mounted it only while open: the behaviour that used
 * to be an effect's job is now the parent's, and this is what says so.
 */
test("the palette opens empty every time", async ({ signedIn: page }) => {
  await page.keyboard.press("ControlOrMeta+k");
  const box = page.getByPlaceholder(/search|jump/i).first();
  await expect(box).toBeVisible();
  await box.fill("Northwood");
  await page.waitForTimeout(800);

  await page.keyboard.press("Escape");
  await page.keyboard.press("ControlOrMeta+k");
  // Not merely blank — the previous search's results must be gone too, which
  // is what the removed effect used to guarantee.
  await expect(page.getByPlaceholder(/search|jump/i).first()).toHaveValue("");
  await expect(page.getByText(/Northwood/i)).toHaveCount(0);
});

test("the palette reaches a page", async ({ signedIn: page }) => {
  await page.keyboard.press("ControlOrMeta+k");
  await page.getByPlaceholder(/search|jump/i).first().fill("Northwood");
  await page.waitForTimeout(1200);

  const hit = page.getByText(/Northwood/i).first();
  if ((await hit.count()) === 0) {
    throw new Error("the palette found nothing for a seeded document");
  }
  await hit.click();
  // The viewer, on a page — which is where the whole archive is aiming.
  await expect(page).toHaveURL(/\/(document|file)\/[^/]+\/page\/\d+/);
});
