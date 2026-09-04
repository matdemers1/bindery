import { render, screen, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Routes, Route, useLocation } from "react-router";

import CommandPalette from "./CommandPalette";

/**
 * Enter, before the debounce has produced a hit, must keep the query (D-02).
 *
 * This is step one of the documented ten-second path, and it used to navigate
 * to `/?q=…` — the landing route, which is Ask, and which reads no `q` at all.
 * The typed words were dropped and the reader landed on an empty front door.
 */
vi.mock("../../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api")>()),
  // Never settles: keeps the palette in the state a fast typist hits, where
  // Enter arrives before any result exists.
  api: { search: vi.fn(() => new Promise(() => {})) },
}));

function Where() {
  const { pathname, search } = useLocation();
  return <output data-testid="where">{pathname + search}</output>;
}

afterEach(cleanup);

describe("the palette's Enter with no result selected", () => {
  it("carries the query to a route that reads it", async () => {
    const user = (await import("@testing-library/dom")).default;
    void user;
    render(
      <MemoryRouter initialEntries={["/archive"]}>
        <Where />
        <Routes>
          <Route path="*" element={<CommandPalette onClose={() => {}} />} />
        </Routes>
      </MemoryRouter>,
    );

    const box = await screen.findByRole("combobox");
    const { fireEvent } = await import("@testing-library/react");
    fireEvent.change(box, { target: { value: "discharge" } });
    fireEvent.keyDown(box, { key: "Enter" });

    const where = screen.getByTestId("where").textContent ?? "";
    expect(where).toContain("q=discharge");
    expect(where.startsWith("/search")).toBe(true);
  });
});

/** A result shaped like the real corpus: one file, two same-titled segments. */
function segment(documentPage: number, filePage: number, count: number, id: string) {
  return {
    document_id: id,
    source_file_id: "f1",
    library_id: "l1",
    title: "Consolidated Service Record - Continuation",
    original_filename: "Army Records 2019.pdf",
    page_start: filePage,
    page_end: filePage + count - 1,
    file_page_count: 100,
    state: "processed",
    received_at: "2026-01-01T00:00:00Z",
    known_form_code: null,
    known_form_name: null,
    matching_pages: 1,
    best_page: {
      page_number: filePage,
      document_page_number: documentPage,
      snippet: "…routine orders filed prior to <mark>discharge</mark>…",
      rank: 1,
    },
  };
}

describe("two segments of one bundle are not the same row (D-04)", () => {
  it("distinguishes them by where they sit in the file", async () => {
    const { api } = await import("../../api");
    vi.mocked(api.search).mockResolvedValue({
      query: "discharge",
      total: 2,
      results: [segment(1, 1, 46, "a"), segment(1, 49, 52, "b")],
      facets: {},
      suggestions: [],
    } as unknown as Awaited<ReturnType<typeof api.search>>);

    const { fireEvent } = await import("@testing-library/react");
    render(
      <MemoryRouter initialEntries={["/archive"]}>
        <Routes>
          <Route path="*" element={<CommandPalette onClose={() => {}} />} />
        </Routes>
      </MemoryRouter>,
    );
    fireEvent.change(await screen.findByRole("combobox"), {
      target: { value: "discharge" },
    });

    const rows = await screen.findAllByRole("option");
    expect(rows).toHaveLength(2);
    const texts = rows.map((r) => r.textContent ?? "");
    // The whole point: identical titles, and the rows still differ.
    expect(texts[0]).not.toBe(texts[1]);
    expect(texts[0]).toContain("page 1 of 100");
    expect(texts[1]).toContain("page 49 of 100");
    expect(texts[0]).toContain("Army Records 2019.pdf");
  });
});
