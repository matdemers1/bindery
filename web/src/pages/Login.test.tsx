import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: { login: vi.fn() },
  versionApi: { report: vi.fn(() => new Promise(() => {})) },
}));

const { api, ApiError } = await import("../api");
const loginMock = vi.mocked(api.login);
const { default: Login } = await import("./Login");

const secondFactor = () => new ApiError(401, "second factor required");

const type = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });

async function toCodeStep(onSignedIn = vi.fn()) {
  loginMock.mockRejectedValueOnce(secondFactor());
  render(<Login onSignedIn={onSignedIn} />);
  type("Email", "owner@example.com");
  type("Password", "quiet harbour lantern");
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  await screen.findByText("Enter your code");
  return onSignedIn;
}

beforeEach(() => loginMock.mockReset());
afterEach(cleanup);

describe("Login — two steps, and the second submits itself", () => {
  it("a wrong password says so without naming which half was wrong", async () => {
    loginMock.mockRejectedValueOnce(new ApiError(401, "invalid credentials"));
    render(<Login onSignedIn={vi.fn()} />);
    type("Email", "owner@example.com");
    type("Password", "not it");
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText(/do not match an account/)).toBeTruthy();
    expect(screen.getByLabelText("Password").getAttribute("aria-invalid")).toBe("true");
  });

  it("filling the last box signs in, with no button to press", async () => {
    const onSignedIn = await toCodeStep();
    loginMock.mockResolvedValueOnce({ id: "u", email: "owner@example.com", display_name: null, is_admin: true });

    type("Six-digit code", "482913");

    await waitFor(() => expect(onSignedIn).toHaveBeenCalledTimes(1));
    expect(loginMock).toHaveBeenLastCalledWith("owner@example.com", "quiet harbour lantern", "482913");
  });

  it("a rejected code is said in words, marked invalid, and cleared for the next try", async () => {
    await toCodeStep();
    loginMock.mockRejectedValueOnce(secondFactor());

    type("Six-digit code", "111111");

    expect(await screen.findByText(/That code was not accepted/)).toBeTruthy();
    const field = screen.getByLabelText("Six-digit code");
    expect(field.getAttribute("aria-invalid")).toBe("true");
    await waitFor(() => expect((field as HTMLInputElement).value).toBe(""), { timeout: 1500 });
  });

  it("a recovery code goes as typed, and the server is left to read it", async () => {
    await toCodeStep();
    fireEvent.click(screen.getByRole("button", { name: /Use a recovery code/ }));
    loginMock.mockResolvedValueOnce({ id: "u", email: "owner@example.com", display_name: null, is_admin: true });

    type("Recovery code", "abcde-fghjk");

    await waitFor(() =>
      expect(loginMock).toHaveBeenLastCalledWith("owner@example.com", "quiet harbour lantern", "ABCDEFGHJK"),
    );
  });

  it("the throttle's delay is said, not a silent refusal", async () => {
    await toCodeStep();
    loginMock.mockRejectedValueOnce(new ApiError(429, "Too many attempts", 42));
    type("Six-digit code", "123456");
    expect(await screen.findByText("Too many attempts. Try again in 42 seconds.")).toBeTruthy();
  });

  it("leads to the reset form with a link you can see is a link", () => {
    render(<Login onSignedIn={vi.fn()} />);
    const link = screen.getByRole("link", { name: "Use a reset code" });
    expect(link.getAttribute("href")).toBe("/reset");
  });
});
