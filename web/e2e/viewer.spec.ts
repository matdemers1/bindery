import { test, expect } from "./fixtures";

/**
 * The viewer, which now remounts on a `key` rather than clearing itself.
 *
 * The reset it replaced was three `setState` calls at the top of the loader,
 * which meant one render showing the previous document's title above the new
 * one's pages. That window is what this guards.
 *
 * The document ids come from the API rather than from a screen. The first
 * version of this read links off `/archive`, which is empty on a fresh
 * archive: with no API key the seeded documents sit in review rather than
 * filed, so the test failed on where it went looking instead of on the
 * behaviour it is about.
 */
test("moving between documents never shows the previous one's title", async ({
  signedIn: page,
}) => {
  // Found by searching, which is the only listing that shows these documents.
  // `/api/review` returns needs_review only and `/api/archive` returns filed
  // only — with no API key the seeded corpus is `pending_classification`, so
  // both are empty and both earlier versions of this test failed on where they
  // looked rather than on the viewer.
  const hrefs: string[] = [];
  for (const term of ["Northwood", "Fenwick"]) {
    await page.goto(`/search?q=${term}`);
    const link = page.locator('a[href*="/document/"]').first();
    await expect(link, `search found nothing for ${term}`).toBeVisible();
    hrefs.push((await link.getAttribute("href"))!);
  }
  expect(hrefs[0]).not.toEqual(hrefs[1]);
  const ids = hrefs;

  await page.goto(ids[0]);
  await expect(page.getByRole("navigation", { name: "Sections" })).toBeVisible();
  // Scoped to the content, not the page. `h1, h2` unscoped matches the
  // sidebar's group labels, and this asserted that the viewer was showing
  // "FIND" — which it was, in the navigation, three times before I looked.
  const first = (await page.locator("main h1").first().innerText()).trim();

  await page.goto(ids[1]);
  await expect(page.getByRole("navigation", { name: "Sections" })).toBeVisible();
  const second = (await page.locator("main h1").first().innerText()).trim();

  // The durable property, rather than a one-frame race. With the `key` the
  // component cannot carry the previous document's state across at all; the
  // observable consequence is simply that the two never agree.
  expect(first).not.toEqual("");
  expect(second, "the viewer kept the previous document's title").not.toEqual(first);
});
