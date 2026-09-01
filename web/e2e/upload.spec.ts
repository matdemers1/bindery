import { test, expect } from "./fixtures";

/**
 * Ingest, end to end: a file the browser hands over must reach the pipeline.
 *
 * The Add screen is a page rather than an OS file dialog on purpose — opening
 * the picker meant everything after it happened somewhere nobody could watch.
 * This test walks the path a person actually takes.
 */
test("a dropped file reaches the pipeline", async ({ signedIn: page }) => {
  await page.goto("/add");
  await expect(page.getByRole("navigation", { name: "Sections" })).toBeVisible();

  const body = `E2E upload probe ${Date.now()}\nThis text exists so OCR has something true to read.`;
  await page.setInputFiles('input[type="file"]', {
    name: "e2e-probe.txt",
    mimeType: "text/plain",
    buffer: Buffer.from(body),
  });

  // Accepted, and visible on the screen that says where things are — the
  // archive's promise is that nothing fails silently (invariant 8).
  await page.waitForTimeout(4000);
  await page.goto("/pipeline");
  await expect(page.getByText("e2e-probe.txt").first()).toBeVisible({ timeout: 20_000 });
});
