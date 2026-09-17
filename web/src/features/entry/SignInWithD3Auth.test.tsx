import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api")>()),
  oidcApi: { status: vi.fn(), link: vi.fn(), unlink: vi.fn() },
}));

const { oidcApi } = await import("../../api");
const statusMock = vi.mocked(oidcApi.status);
const { SignInWithD3Auth } = await import("./SignInWithD3Auth");

beforeEach(() => statusMock.mockClear());
afterEach(cleanup);

describe("Sign in with D3 Auth — the button, and when there is no button", () => {
  it("says nothing at all about a provider nobody configured", async () => {
    statusMock.mockResolvedValue({ mode: "off", ready: false, issuer: null });
    render(<SignInWithD3Auth />);
    await waitFor(() => expect(statusMock).toHaveBeenCalled());
    expect(screen.queryByText(/D3 Auth/)).toBeNull();
  });

  it("offers the way in when the provider is there", async () => {
    statusMock.mockResolvedValue({ mode: "optional", ready: true, issuer: "https://auth.example.test" });
    render(<SignInWithD3Auth />);
    const link = await screen.findByRole("link", { name: /Sign in with D3 Auth/ });
    expect(link.getAttribute("href")).toBe("/api/auth/oidc/start");
  });

  it("offers nothing rather than something broken when the provider is down and SSO is optional", async () => {
    statusMock.mockResolvedValue({ mode: "optional", ready: false, issuer: "https://auth.example.test" });
    render(<SignInWithD3Auth />);
    await waitFor(() => expect(statusMock).toHaveBeenCalled());
    expect(screen.queryByRole("link", { name: /Sign in with D3 Auth/ })).toBeNull();
    expect(screen.queryByText(/unavailable/i)).toBeNull();
  });

  it("says why when SSO is required and the provider is down", async () => {
    // Here it *is* the way in, so silence would read as a broken page.
    statusMock.mockResolvedValue({ mode: "required", ready: false, issuer: "https://auth.example.test" });
    render(<SignInWithD3Auth />);
    expect(await screen.findByText("D3 Auth is unavailable")).toBeTruthy();
    expect(screen.queryByRole("link", { name: /Sign in with D3 Auth/ })).toBeNull();
    expect(screen.getByText(/sign in with a password below/)).toBeTruthy();
  });

  // Not covered here: the archive itself failing to answer /status. The component catches it and
  // renders nothing, but a rejection crossing a vi.fn() is reported as the test's own failure by
  // vitest's settled-result tracking even when the caller catches it. The e2e suite covers it
  // against a real server, where the request genuinely fails.
});
