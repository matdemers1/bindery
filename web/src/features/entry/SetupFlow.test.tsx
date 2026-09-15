import { StrictMode } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api")>()),
  setupApi: { state: vi.fn(), claim: vi.fn(), complete: vi.fn() },
  accountsApi: { totpStart: vi.fn(), totpConfirm: vi.fn() },
  versionApi: { report: vi.fn(() => new Promise(() => {})) },
}));

const { setupApi, accountsApi, ApiError } = await import("../../api");
const { default: SetupFlow } = await import("./SetupFlow");

const type = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });

const OWNER = { id: "u", email: "owner@example.com", display_name: null, is_admin: false };

beforeEach(() => {
  vi.mocked(setupApi.claim).mockReset();
  vi.mocked(setupApi.complete).mockReset();
  vi.mocked(accountsApi.totpStart).mockReset();
  vi.mocked(accountsApi.totpConfirm).mockReset();
});
afterEach(cleanup);

describe("SetupFlow · claim", () => {
  function fill(code = "abcd-efgh-jkmn") {
    type("Setup code", code);
    type("Email", "owner@example.com");
    type("Password", "quiet harbour lantern");
  }

  it("sends the code as the boxes hold it, and moves on once claimed", async () => {
    const onChanged = vi.fn(async () => {});
    vi.mocked(setupApi.claim).mockResolvedValueOnce(OWNER);
    render(<SetupFlow stage="claim" onChanged={onChanged} />);
    fill();
    fireEvent.click(screen.getByRole("button", { name: "Create the owner account" }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(setupApi.claim).toHaveBeenCalledWith({
      code: "ABCDEFGHJKMN",
      email: "owner@example.com",
      password: "quiet harbour lantern",
      display_name: null,
      library_name: "Personal",
    });
  });

  it("a wrong code is an error on the code field, and nothing moves on", async () => {
    const onChanged = vi.fn(async () => {});
    vi.mocked(setupApi.claim).mockRejectedValueOnce(new ApiError(400, "that setup code is not valid"));
    render(<SetupFlow stage="claim" onChanged={onChanged} />);
    fill();
    fireEvent.click(screen.getByRole("button", { name: "Create the owner account" }));

    expect(await screen.findByText(/setup code is not valid/)).toBeTruthy();
    expect(screen.getByLabelText("Setup code").getAttribute("aria-invalid")).toBe("true");
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("a short code never reaches the server", () => {
    render(<SetupFlow stage="claim" onChanged={vi.fn(async () => {})} />);
    fill("ABCD");
    fireEvent.click(screen.getByRole("button", { name: "Create the owner account" }));
    expect(screen.getByText("The setup code is twelve characters.")).toBeTruthy();
    expect(setupApi.claim).not.toHaveBeenCalled();
  });

  it("the server's password rule is shown on the password field", async () => {
    vi.mocked(setupApi.claim).mockRejectedValueOnce(new ApiError(422, "Do not put your email address in your password."));
    render(<SetupFlow stage="claim" onChanged={vi.fn(async () => {})} />);
    fill();
    fireEvent.click(screen.getByRole("button", { name: "Create the owner account" }));
    expect(await screen.findByText("Do not put your email address in your password.")).toBeTruthy();
    expect(screen.getByLabelText("Password").getAttribute("aria-invalid")).toBe("true");
  });
});

describe("SetupFlow · secure and finish", () => {
  it("enrols once, then will not finish until the codes are saved", async () => {
    vi.mocked(accountsApi.totpStart).mockResolvedValue({
      secret: "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
      uri: "otpauth://totp/Bindery:owner",
      qr_svg: '<svg data-testid="qr"><path d="M0 0"/></svg>',
    });
    vi.mocked(accountsApi.totpConfirm).mockResolvedValueOnce(["abcde-fghjk", "mnpqr-stuvw"]);
    vi.mocked(setupApi.complete).mockResolvedValueOnce({ ...OWNER, is_admin: true });
    const onChanged = vi.fn(async () => {});

    // StrictMode mounts effects twice, which is exactly the double start that
    // would replace the secret behind the QR code already on screen.
    render(
      <StrictMode>
        <SetupFlow stage="secure" onChanged={onChanged} />
      </StrictMode>,
    );
    expect(await screen.findByTestId("qr")).toBeTruthy();
    expect(screen.getByText("JBSW Y3DP EHPK 3PXP JBSW Y3DP EHPK 3PXP")).toBeTruthy();
    expect(accountsApi.totpStart).toHaveBeenCalledTimes(1);

    type("Code from the app", "482913");
    expect(await screen.findByText("Save your recovery codes", {}, { timeout: 2000 })).toBeTruthy();
    expect(accountsApi.totpConfirm).toHaveBeenCalledWith("482913");

    const open = screen.getByRole("button", { name: "Open the archive" });
    expect((open as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("checkbox"));
    await waitFor(() => expect((open as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(open);
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(setupApi.complete).toHaveBeenCalledTimes(1);
  });

  it("an owner whose two-factor is already on goes straight to finishing", async () => {
    vi.mocked(accountsApi.totpStart).mockRejectedValueOnce(new ApiError(409, "two-factor is already on"));
    render(<SetupFlow stage="secure" onChanged={vi.fn(async () => {})} />);
    expect(await screen.findByText("Finish setting up")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Open the archive" }) as HTMLButtonElement).disabled).toBe(false);
  });
});
