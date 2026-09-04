import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import { LiveProvider } from "../../live/LiveProvider";
import PhotosPage from "./PhotosPage";
import type { Photo, PhotoWall } from "../../api";

/**
 * Which tab the photo wall opens on, and what it shows once you choose one
 * (CR-123).
 *
 * This is a decision the screen makes for you, from two numbers it has to ask
 * the server for, and it is invisible when it goes wrong: a wall showing
 * photographs under a tab labelled Videos looks like an empty archive rather
 * than a bug. Until now the only thing that ever exercised it was a Playwright
 * spec against one seeded corpus, which has exactly one shape of archive in it.
 */

vi.mock("../../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api")>()),
  api: { photos: vi.fn(), reclassify: vi.fn() },
}));

const { api } = await import("../../api");
const photosMock = vi.mocked(api.photos);

function photo(kind: "image" | "video", title: string): Photo {
  return {
    document_id: `d-${title}`,
    source_file_id: `f-${title}`,
    page: 1,
    title,
    summary: null,
    original_filename: `${title}.${kind === "video" ? "mp4" : "jpg"}`,
    received_at: "2026-01-01T00:00:00Z",
    document_date: null,
    text_chars: 0,
    described: true,
    kind,
    media: null,
  };
}

/**
 * Stands in for the server: an archive holding these images and these videos.
 *
 * Answers from the parameters of the request rather than from call order,
 * because the point of the fix under test is that the screen no longer makes a
 * fixed number of calls in a fixed sequence.
 */
function archiveOf(images: string[], videos: string[]) {
  photosMock.mockImplementation(async (params = {}): Promise<PhotoWall> => {
    const titles = params.kind === "video" ? videos : images;
    const kind = params.kind === "video" ? "video" : "image";
    return {
      total: titles.length,
      photos: (params.limit ?? 120) === 1 ? [] : titles.map((t) => photo(kind, t)),
    };
  });
}

function renderWall() {
  return render(
    <MemoryRouter>
      <LiveProvider>
        <PhotosPage />
      </LiveProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
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

describe("PhotosPage", () => {
  it("opens on Photos when there are photographs", async () => {
    archiveOf(["Passport photo"], ["Driveway"]);
    renderWall();
    expect(await screen.findByAltText("Passport photo")).toBeTruthy();
    expect(screen.getByRole("tab", { name: /^Photos/ }).getAttribute("aria-selected")).toBe("true");
  });

  it("opens on Videos when every picture in the archive is a video", async () => {
    // The reason the tab is chosen rather than fixed: opening on an empty
    // Photos tab reads as "the archive is empty".
    archiveOf([], ["Driveway"]);
    renderWall();
    expect(await screen.findByRole("tab", { name: /^Videos/, selected: true })).toBeTruthy();
    expect(await screen.findByText("Driveway")).toBeTruthy();
  });

  it("shows the videos once you choose the Videos tab", async () => {
    archiveOf(["Passport photo"], ["Driveway"]);
    renderWall();
    await screen.findByAltText("Passport photo");

    // `mouseDown`, not `click`: Radix activates a tab on mouse-down, so a
    // synthetic click alone never reaches it.
    fireEvent.mouseDown(screen.getByRole("tab", { name: /^Videos/ }));

    expect(await screen.findByText("Driveway")).toBeTruthy();
    expect(screen.queryByAltText("Passport photo")).toBeNull();
  });

  it("asks twice for a wall, not three times", async () => {
    // The wall used to cost a COUNT for each tab plus the wall itself, all of
    // it re-run on every pipeline hint. Two of the three asked the same
    // question. This is the assertion that keeps the third one from coming
    // back the next time somebody needs a number on a tab.
    archiveOf(["Passport photo"], ["Driveway"]);
    renderWall();
    await screen.findByAltText("Passport photo");

    expect(photosMock).toHaveBeenCalledTimes(2);
    expect(photosMock.mock.calls.filter(([p]) => p?.limit === 1)).toHaveLength(1);
  });

  it("says how many of each kind there are on the tab that holds them", async () => {
    archiveOf(["a", "b", "c"], ["d"]);
    renderWall();
    // The count carries its unit into the accessible name. It used to sit in
    // its own span beside the label, so the name was the two run together —
    // "Photos 3", which is a label and a loose number rather than a count.
    expect(await screen.findByRole("tab", { name: "Photos, 3 items" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Videos, 1 item" })).toBeTruthy();
  });
});
