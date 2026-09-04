/**
 * One grammar for the one fact this product exists to state.
 *
 * "Page numbers are always disambiguated" is a non-negotiable rule in the
 * vault, and it is the rule the whole product turns on: a document here is a
 * page range over a file, so "page 1" is ambiguous until you say page 1 *of
 * what*. Three surfaces stated that fact three different ways — the viewer
 * header as "Page 1 of 2 · page 47 of 100 in …", search as "Page 1 of this
 * document · page 47 of 100 in …", and the ⌘K palette as a bare "p.1" that
 * omitted the file entirely. The palette's version is why two segments of the
 * same bundle rendered as two identical rows, and a wrong pick there lands on
 * a real, plausible document and looks like success (D-04).
 *
 * Returned in parts rather than as one string because the filename is a link
 * on two of the three surfaces and plain text on the third.
 */
export interface PageFact {
  /** Where the page sits inside the document. */
  documentPage: number;
  /** How many pages the document has, when the caller knows. */
  documentPageCount?: number | null;
  /** Where the same page sits inside the underlying file. */
  filePage: number;
  /** How many pages the file has, when the caller knows. */
  filePageCount?: number | null;
  filename?: string | null;
}

export interface PageParts {
  /** Everything up to the filename, e.g. "page 1 of 2 in this document · page 47 of 100 in". */
  lead: string;
  /** The filename, or null when the document is the whole file and there is nothing to add. */
  filename: string | null;
}

/**
 * True when the document covers the entire file, in which case naming the file
 * again says nothing — "page 3 of 3 · page 3 of 3 in scan.pdf" is noise.
 */
function isWholeFile(fact: PageFact): boolean {
  return (
    fact.documentPage === fact.filePage &&
    fact.documentPageCount != null &&
    fact.filePageCount != null &&
    fact.documentPageCount === fact.filePageCount
  );
}

export function pageParts(fact: PageFact): PageParts {
  const withinDocument =
    fact.documentPageCount != null
      ? `page ${fact.documentPage} of ${fact.documentPageCount} in this document`
      : `page ${fact.documentPage} of this document`;

  if (isWholeFile(fact) || !fact.filename) {
    return { lead: withinDocument, filename: null };
  }

  const withinFile =
    fact.filePageCount != null
      ? `page ${fact.filePage} of ${fact.filePageCount} in`
      : `page ${fact.filePage} of`;

  return { lead: `${withinDocument} · ${withinFile}`, filename: fact.filename };
}

/** The whole fact as one string, for surfaces that cannot hold a link. */
export function pageLabel(fact: PageFact): string {
  const { lead, filename } = pageParts(fact);
  return filename ? `${lead} ${filename}` : lead;
}
