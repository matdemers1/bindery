import { test, expect } from "./fixtures";

/**
 * Sign in with D3 Auth, as it is for an archive that has not configured it (Phase 20).
 *
 * CI runs Bindery alone — there is no provider on the network — so what these assert is the
 * half that matters most for everybody else: an archive whose operator never set SSO up says
 * nothing about it anywhere, and its routes are not there to be poked at.
 *
 * The flow against a real provider is exercised in `tests/test_oidc_routes.py` with a stand-in,
 * and by hand against auth.d3cloud.io before each deploy. What is deliberately *not* claimed
 * here is that the real round trip works; that claim would need a provider in the compose file.
 */
test.describe(() => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("an archive with no provider configured never mentions one", async ({ page }) => {
    await page.goto("/login");
    await expect(page.getByLabel("Email")).toBeVisible();

    await expect(page.getByRole("link", { name: /D3 Auth/i })).toHaveCount(0);
    await expect(page.getByText(/D3 Auth/i)).toHaveCount(0);
    await expect(page.getByText(/unavailable/i)).toHaveCount(0);
  });

  test("the status route says off, and says nothing else", async ({ request }) => {
    const response = await request.get("/api/auth/oidc/status");
    expect(response.status()).toBe(200);
    expect(await response.json()).toEqual({ mode: "off", ready: false, issuer: null });
  });

  test("the sign-in and callback routes are not there while SSO is off", async ({ request }) => {
    expect((await request.get("/api/auth/oidc/start", { maxRedirects: 0 })).status()).toBe(404);
    expect((await request.get("/api/auth/oidc/callback?code=x&state=y")).status()).toBe(404);
    expect(
      (await request.post("/api/auth/oidc/backchannel-logout", { form: { logout_token: "x" } })).status(),
    ).toBe(404);
  });
});

test("Settings offers no connection to a provider that is not configured", async ({ signedIn: page }) => {
  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await expect(page.getByText("Sign in with D3 Auth")).toHaveCount(0);
});
