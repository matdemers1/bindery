import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";

import ViewerPage from "./ViewerPage";

/**
 * Find-next inside a bundle (D-03).
 *
 * The product's whole claim is a term buried in a hundred-page bundle. It
 * found the first occurrence and then abandoned the reader: search counted the
 * others — "1 more matching page" — and nothing could reach them, because
 * Prev/Next walk the *current document's* pages and the other matches are
 * usually in a different constituent document of the same file.
 */
const DOCUMENT = {
  document: {
    id: "d1",
    source_file_id: "f1",
    library_id: "l1",
    title: "Department of Defense - DD-214 - Discharge",
    page_start: 47,
    page_end: 48,
    summary: null,
    document_date: null,
    sensitivity: "normal",
    redundancy: "unique",
    review_state: "filed",
    is_backlog: false,
    known_form_id: null,
    correspondent_id: null,
    document_type_id: null,
    created_at: "2026-01-01T00:00:00Z",
  },
  known_form: { code: "DD-214", name: "Certificate of Release" },
  // The document's own pages: 47 and 48. Prev/Next are bounded by these, which
  // is precisely why a match on page 63 was unreachable before D-03.
  pages: [
    { page_number: 47, render_path: "derived/x/pages/47.webp", thumb_path: "derived/x/thumbs/47.webp" },
    { page_number: 48, render_path: "derived/x/pages/48.webp", thumb_path: "derived/x/thumbs/48.webp" },
  ],
  fields: [],
  tags: [],
};

/** The viewer chains `document` -> `file`; `detail` is the *file*. */
const FILE = {
  source_file: {
    id: "f1",
    library_id: "l1",
    sha256: "a".repeat(64),
    byte_size: 1024,
    mime_type: "application/pdf",
    original_filename: "Army Records 2019.pdf",
    ingest_source: "watched_folder",
    page_count: 100,
    state: "processed",
    received_at: "2026-01-01T00:00:00Z",
  },
  pages: Array.from({ length: 100 }, (_, i) => ({
    page_number: i + 1,
    render_path: `derived/x/pages/${i + 1}.webp`,
    thumb_path: `derived/x/thumbs/${i + 1}.webp`,
  })),
};

vi.mock("../../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api")>()),
  api: {
    document: vi.fn(),
    file: vi.fn(),
    fileMatches: vi.fn(),
    pageBoxes: vi.fn(() => new Promise(() => {})),
    why: vi.fn(() => new Promise(() => {})),
  },
}));

const { api } = await import("../../api");

function Where() {
  const { pathname, search } = useLocation();
  return <output data-testid="where">{pathname + search}</output>;
}

function open() {
  return render(
    <MemoryRouter initialEntries={["/document/d1/page/1?q=discharge"]}>
      <Where />
      <Routes>
        <Route path="/document/:documentId/page/:pageNumber" element={<ViewerPage mode="document" />} />
        <Route path="/file/:fileId/page/:pageNumber" element={<ViewerPage mode="file" />} />
      </Routes>
    </MemoryRouter>,
  );
}

// jsdom does not implement scrollIntoView, and the thumbnail rail calls it to
// keep the current page visible. Without this the whole viewer unmounts mid-
// test and every query fails with a message about the element it was looking
// for rather than the reason it is not there.
beforeAll(() => {
  Element.prototype.scrollIntoView = () => {};
});

afterEach(cleanup);

describe("stepping through matches inside one file", () => {
  it("reaches a match in a different constituent document", async () => {
    vi.mocked(api.document).mockResolvedValue(
      DOCUMENT as unknown as Awaited<ReturnType<typeof api.document>>,
    );
    vi.mocked(api.file).mockResolvedValue(
      FILE as unknown as Awaited<ReturnType<typeof api.file>>,
    );
    // The DD-214 is pages 47-48. Page 63 is inside the *other* continuation
    // record — unreachable by Prev/Next, which stop at 48.
    vi.mocked(api.fileMatches).mockResolvedValue({
      query: "discharge",
      pages: [47, 48, 63],
    });

    open();

    expect(await screen.findByText("Match 1 of 3")).toBeTruthy();
    expect(await screen.findByText("Match 1 of 3")).toBeTruthy();
    // 47 -> 48 -> 63. The third is inside the *other* continuation record, so
    // it is unreachable with Prev/Next, which stop at the document's page 48.
    fireEvent.click(screen.getByRole("button", { name: "Next match" }));
    // Crossing into file mode remounts and refetches, so wait for the counter
    // to catch up rather than clicking into a loading state.
    expect(await screen.findByText("Match 2 of 3")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Next match" }));
    expect(await screen.findByText("Match 3 of 3")).toBeTruthy();

    const where = screen.getByTestId("where").textContent ?? "";
    expect(where).toContain("/file/f1/page/63");
    expect(where).toContain("q=discharge");
  });

  it("shows no stepper when the query matches nothing", async () => {
    vi.mocked(api.document).mockResolvedValue(
      DOCUMENT as unknown as Awaited<ReturnType<typeof api.document>>,
    );
    vi.mocked(api.file).mockResolvedValue(
      FILE as unknown as Awaited<ReturnType<typeof api.file>>,
    );
    vi.mocked(api.fileMatches).mockResolvedValue({ query: "discharge", pages: [] });
    open();

    await screen.findByText(/Discharge/);
    expect(screen.queryByRole("button", { name: "Next match" })).toBeNull();
  });
});
