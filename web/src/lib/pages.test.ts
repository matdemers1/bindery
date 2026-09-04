import { describe, expect, it } from "vitest";

import { pageLabel, pageParts } from "./pages";

describe("pageLabel — the disambiguation rule, in one place", () => {
  it("names both coordinates for a document inside a bundle", () => {
    // The canonical case: the DD-214 is pages 47-48 of a 100-page file.
    expect(
      pageLabel({
        documentPage: 1,
        documentPageCount: 2,
        filePage: 47,
        filePageCount: 100,
        filename: "Army Records 2019.pdf",
      }),
    ).toBe("page 1 of 2 in this document · page 47 of 100 in Army Records 2019.pdf");
  });

  it("tells two segments of one file apart", () => {
    // The failure this exists to prevent: two documents, same title, same
    // document-relative page number, different places in the file.
    const first = pageLabel({
      documentPage: 1, documentPageCount: 46, filePage: 1, filePageCount: 100,
      filename: "Army Records 2019.pdf",
    });
    const second = pageLabel({
      documentPage: 1, documentPageCount: 52, filePage: 49, filePageCount: 100,
      filename: "Army Records 2019.pdf",
    });
    expect(first).not.toBe(second);
    expect(second).toContain("page 49 of 100");
  });

  it("does not say the same thing twice when the document is the whole file", () => {
    expect(
      pageLabel({
        documentPage: 3, documentPageCount: 3, filePage: 3, filePageCount: 3,
        filename: "receipt.pdf",
      }),
    ).toBe("page 3 of 3 in this document");
  });

  it("degrades when a count is unknown rather than inventing one", () => {
    expect(
      pageLabel({ documentPage: 2, filePage: 48, filename: "Army Records 2019.pdf" }),
    ).toBe("page 2 of this document · page 48 of Army Records 2019.pdf");
  });

  it("splits at the filename so a caller can link it", () => {
    const parts = pageParts({
      documentPage: 1, documentPageCount: 2, filePage: 47, filePageCount: 100,
      filename: "Army Records 2019.pdf",
    });
    expect(parts.lead.endsWith("in")).toBe(true);
    expect(parts.filename).toBe("Army Records 2019.pdf");
  });
});
