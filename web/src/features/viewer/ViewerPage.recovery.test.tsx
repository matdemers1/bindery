import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router";

import { ApiError } from "../../api";
import ViewerPage from "./ViewerPage";

/**
 * A failed link is not a dead end (D-06).
 *
 * Both failure branches rendered one centred grey sentence: no way back, no
 * retry, and no distinction between "the fetch failed" and "this is not
 * yours" — while `components/States.tsx` shipped `Empty` and `ErrorState` for
 * exactly this and was imported by nobody.
 */
vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    api: {
      document: vi.fn(),
      file: vi.fn(),
      fileMatches: vi.fn(() => Promise.resolve({ query: "", pages: [] })),
      pageBoxes: vi.fn(() => new Promise(() => {})),
      why: vi.fn(() => new Promise(() => {})),
    },
  };
});

const { api } = await import("../../api");

beforeAll(() => {
  Element.prototype.scrollIntoView = () => {};
});
afterEach(cleanup);

function open() {
  return render(
    <MemoryRouter initialEntries={["/document/gone/page/1"]}>
      <Routes>
        <Route
          path="/document/:documentId/page/:pageNumber"
          element={<ViewerPage mode="document" />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

describe("the viewer's failure branches", () => {
  it("offers a way onward from a 404 without claiming why", async () => {
    vi.mocked(api.document).mockRejectedValue(new ApiError(404, "Not found"));
    open();

    expect(await screen.findByText("Not here")).toBeTruthy();
    expect(screen.getByRole("link", { name: /Search the archive/ })).toBeTruthy();
    // The API answers 404 rather than 403 for a library you are not in, so
    // this screen must not resolve the ambiguity it is designed to preserve.
    expect(screen.queryByText(/Not yours to see/)).toBeNull();
  });

  it("offers a retry for a failure that is worth retrying, and it works", async () => {
    const detail = {
      source_file: {
        id: "f1", library_id: "l1", sha256: "a".repeat(64), byte_size: 1,
        mime_type: "application/pdf", original_filename: "scan.pdf",
        ingest_source: "web_upload", page_count: 1, state: "processed",
        received_at: "2026-01-01T00:00:00Z",
      },
      pages: [{ page_number: 1, render_path: "r", thumb_path: "t" }],
    };
    vi.mocked(api.document)
      .mockRejectedValueOnce(new ApiError(503, "the archive is briefly unavailable"))
      .mockResolvedValue({
        document: {
          id: "d1", source_file_id: "f1", library_id: "l1", title: "Deed",
          page_start: 1, page_end: 1, summary: null, document_date: null,
          sensitivity: "normal", redundancy: "unique", review_state: "filed",
          is_backlog: false, known_form_id: null, correspondent_id: null,
          document_type_id: null, created_at: "2026-01-01T00:00:00Z",
        },
        known_form: null,
        pages: [{ page_number: 1, render_path: "r", thumb_path: "t" }],
        fields: [],
        tags: [],
      } as unknown as Awaited<ReturnType<typeof api.document>>);
    vi.mocked(api.file).mockResolvedValue(
      detail as unknown as Awaited<ReturnType<typeof api.file>>,
    );

    open();

    // The server's own words, not a generic apology.
    expect(await screen.findByText(/briefly unavailable/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Try again/ }));

    expect(await screen.findByText("Deed")).toBeTruthy();
  });
});
