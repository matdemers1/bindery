import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import VaultPage from "./VaultPage";
import type { VaultItem, VaultState } from "../../api";

/**
 * What the vault screen shows, shut and open (ADR-012, CR-123).
 *
 * Two decisions live here and neither is visible in a diff. While the vault is
 * shut the screen may say that it exists and nothing else — a count is already
 * a statement about the contents. While it is open it picks a tab for you, and
 * picking the wrong one shows an empty list for a vault that is not empty,
 * which is the single most alarming thing this screen could do.
 */

vi.mock("../../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api")>()),
  api: { vault: vi.fn(), vaultItems: vi.fn() },
}));

const { api } = await import("../../api");
const vaultMock = vi.mocked(api.vault);
const itemsMock = vi.mocked(api.vaultItems);

function item(title: string, shape: "document" | "photo" | "video"): VaultItem {
  return {
    document_id: `d-${title}`,
    title,
    original_filename: `${title}.bin`,
    byte_size: 1024,
    page_count: 1,
    vaulted_at: "2026-01-01T00:00:00Z",
    warnings: [],
    media_type: null,
    is_image: shape !== "document",
    is_video: shape === "video",
    media: null,
  };
}

function open(...items: VaultItem[]) {
  const state: VaultState = {
    exists: true,
    unlocked: true,
    pin_enabled: false,
    pin_failures: 0,
  };
  vaultMock.mockResolvedValue(state);
  itemsMock.mockResolvedValue(items);
}

function renderVault() {
  return render(
    <MemoryRouter>
      <VaultPage />
    </MemoryRouter>,
  );
}

afterEach(cleanup);

describe("VaultPage", () => {
  it("says only that the vault exists while it is shut", async () => {
    vaultMock.mockResolvedValue({
      exists: true,
      unlocked: false,
      pin_enabled: false,
      pin_failures: 0,
    });
    itemsMock.mockResolvedValue([item("Deed", "document")]);

    renderVault();
    expect(await screen.findByText("The vault is locked.")).toBeTruthy();

    // ADR-012: nothing announces what is inside. No tabs, therefore no counts,
    // and the items call is not even made.
    expect(screen.queryAllByRole("tab")).toHaveLength(0);
    expect(screen.queryByText("Deed")).toBeNull();
    expect(itemsMock).not.toHaveBeenCalled();
  });

  it("opens on Documents when there are documents", async () => {
    open(item("Deed", "document"), item("Beach", "photo"));
    renderVault();
    expect(
      await screen.findByRole("tab", { name: /^Documents,/, selected: true }),
    ).toBeTruthy();
    expect(screen.getByText("Deed")).toBeTruthy();
  });

  it("opens on Photos when everything in the vault is a photograph", async () => {
    // Opening on an empty Documents tab reads as an empty vault, which for
    // this screen is the wrong thing to say to somebody twice over.
    open(item("Beach", "photo"));
    renderVault();
    expect(
      await screen.findByRole("tab", { name: /^Photos,/, selected: true }),
    ).toBeTruthy();
  });

  it("opens on Videos when that is all there is", async () => {
    open(item("Driveway", "video"));
    renderVault();
    expect(
      await screen.findByRole("tab", { name: /^Videos,/, selected: true }),
    ).toBeTruthy();
  });

  it("offers no Videos tab when there are no videos", async () => {
    // A tab that is always empty is furniture.
    //
    // The names here are capitalised because they are now real labels. They
    // used to be lowercase text wearing a `capitalize` class, which styles the
    // pixels and never reaches the accessible name — so this screen read out
    // "documents 12" while showing "Documents 12".
    open(item("Deed", "document"), item("Beach", "photo"));
    renderVault();
    await screen.findByRole("tab", { name: /^Documents,/ });
    expect(screen.queryByRole("tab", { name: /^Videos,/ })).toBeNull();
  });

  it("offers to fill an empty vault rather than showing empty tabs", async () => {
    open();
    renderVault();
    expect(await screen.findByText(/Nothing is in the vault yet/)).toBeTruthy();
    expect(screen.queryAllByRole("tab")).toHaveLength(0);
  });
});
