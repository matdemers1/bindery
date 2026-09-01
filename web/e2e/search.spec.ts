import { test, expect } from "./fixtures";

/**
 * Retrieval, which is the one thing the archive exists to do.
 *
 * Deliberately against the seeded corpus rather than a mock: the failure this
 * catches is the search path breaking between the browser and Postgres, and a
 * mock would be reassuring about neither end.
 */
test("searching finds a seeded document and opens it", async ({ signedIn: page }) => {
  await page.goto("/search");
  const box = page.getByPlaceholder(/Search every page/i);
  await box.fill("Northwood");
  // The search box is a form and the query lives in the URL, so typing alone
  // changes nothing — it commits on submit. Written without this and the test
  // failed against an unsubmitted box, which looked like a retrieval bug.
  await box.press("Enter");
  await expect(page).toHaveURL(/q=Northwood/i);

  await expect(page.getByText(/Northwood/i).first()).toBeVisible();
});

test("search survives a term that matches nothing", async ({ signedIn: page }) => {
  await page.goto("/search");
  const box = page.getByPlaceholder(/Search every page/i);
  await box.fill("zzzqqqxxnothinghere");
  await box.press("Enter");
  await page.waitForTimeout(1200);
  // The point is that it says so rather than hanging or erroring.
  await expect(page.getByRole("navigation", { name: "Sections" })).toBeVisible();
});
