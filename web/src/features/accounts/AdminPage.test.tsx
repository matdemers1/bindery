import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import AdminPage from "./AdminPage";
import type { AdminAccount } from "../../api";
import { LiveProvider } from "../../live/LiveProvider";

/**
 * The People screen and an account waiting out its deletion (BND-ADR-015,
 * BND-T-23.5).
 *
 * Someone who deletes their account from D3 Constellation is suspended at once
 * and purged a week later unless an administrator restores them. Until this
 * screen said so, that row was indistinguishable from an ordinary suspension —
 * and Restore, the one control that can still undo the deletion, read as an
 * un-suspend rather than the last chance it is.
 */

vi.mock("../../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api")>()),
  accountsApi: { accounts: vi.fn(), invitations: vi.fn() },
}));

const { accountsApi } = await import("../../api");
const accountsMock = vi.mocked(accountsApi.accounts);
const invitationsMock = vi.mocked(accountsApi.invitations);

function account(email: string, overrides: Partial<AdminAccount> = {}): AdminAccount {
  return {
    id: `id-${email}`,
    email,
    display_name: null,
    is_admin: false,
    is_active: true,
    suspended_at: null,
    locked_until: null,
    delete_after: null,
    totp_enabled: true,
    storage_quota_bytes: null,
    used_bytes: 0,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function rowOf(email: string): HTMLElement {
  const row = screen.getByText(email).closest("tr");
  if (!row) throw new Error(`no row for ${email}`);
  return row;
}

beforeEach(() => {
  invitationsMock.mockResolvedValue([]);
  // jsdom would open a real socket to ws://localhost/api/live and then retry on
  // a timer. The socket is not what is under test.
  vi.stubGlobal(
    "WebSocket",
    class {
      close() {}
    },
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("AdminPage", () => {
  it("says when an account's deletion runs and that Restore cancels it", async () => {
    const deleteAfter = "2026-10-11T15:00:00Z";
    accountsMock.mockResolvedValue([
      account("leaving@example.test", {
        is_active: false,
        suspended_at: "2026-10-04T15:00:00Z",
        delete_after: deleteAfter,
      }),
      account("suspended@example.test", {
        is_active: false,
        suspended_at: "2026-10-01T00:00:00Z",
      }),
    ]);

    render(
      <MemoryRouter>
        <LiveProvider>
          <AdminPage />
        </LiveProvider>
      </MemoryRouter>,
    );
    await screen.findByText("leaving@example.test");

    const date = new Date(deleteAfter).toLocaleDateString(undefined, { dateStyle: "medium" });
    const leaving = within(rowOf("leaving@example.test"));
    expect(leaving.getByText(`Deleting on ${date} — Restore cancels it`)).toBeTruthy();
    expect(leaving.getByText("deletion scheduled")).toBeTruthy();
    // The control the notice names is on the same row.
    expect(leaving.getByRole("button", { name: /Restore/ })).toBeTruthy();
    // And it replaces the plain state, rather than sitting beside a "suspended"
    // that would read as the whole story.
    expect(leaving.queryByText("suspended")).toBeNull();

    // An ordinary suspension says nothing about deleting.
    const suspended = within(rowOf("suspended@example.test"));
    expect(suspended.getByText("suspended")).toBeTruthy();
    expect(suspended.queryByText(/Deleting on/)).toBeNull();
    expect(suspended.queryByText("deletion scheduled")).toBeNull();
  });
});
