import { test, expect } from "./fixtures";

/**
 * The Trust screen, which exists so one question is answerable without SSH:
 * could I get my documents back?
 */
test("the offsite panel is honest about not being configured", async ({ signedIn: page }) => {
  await page.goto("/trust");
  await page.getByRole("button", { name: "Export & resilience" }).click();

  const offsite = page.locator("section").filter({
    has: page.getByRole("heading", { name: "Offsite copy" }),
  });
  await expect(offsite.first()).toBeVisible();
  // Never a green tick on an archive that has never sent a copy anywhere.
  await expect(offsite.first().getByText(/Not configured/i)).toBeVisible();
  // And no button, rather than a disabled one. There is nowhere to replicate
  // to, so offering the action greyed out would be describing a capability the
  // archive does not have. Written expecting `toBeDisabled` and corrected to
  // what the screen actually does, which is the better of the two.
  await expect(offsite.first().getByRole("button", { name: "Replicate now" })).toHaveCount(0);
});

test("the health tab renders without a key or a network", async ({ signedIn: page }) => {
  await page.goto("/trust");
  await expect(page.getByRole("button", { name: "Health" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Sections" })).toBeVisible();
});
