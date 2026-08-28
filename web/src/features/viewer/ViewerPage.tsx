import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";

import {
  ApiError,
  api,
  type DocumentDetail,
  type PageBoxes,
  type SourceFileDetail,
  fileUrl,
} from "../../api";
import { matchesTerm, queryTerms } from "../../lib/highlight";

/**
 * Renders a page range as if it were a standalone document (ADR-001), while
 * never hiding that it came out of a larger file. Every page number is shown
 * both ways (REQ-030) — "page 2 of this document, page 48 of the file" — because
 * a bundle segment that pretends to be a whole file is a document you can't
 * cross-reference against the paper original.
 *
 * Two modes: a document (the normal path, reached from search) and a whole file
 * (reached from the pipeline screen or the segmentation editor).
 */
export default function ViewerPage({ mode }: { mode: "document" | "file" }) {
  const params_ = useParams();
  const routeId = (mode === "document" ? params_.documentId : params_.fileId) ?? "";
  const pageNumber = params_.pageNumber ?? "1";
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const query = params.get("q") ?? "";
  const localPage = Math.max(1, Number(pageNumber) || 1);

  const [detail, setDetail] = useState<SourceFileDetail | null>(null);
  const [document_, setDocument] = useState<DocumentDetail | null>(null);
  const [boxes, setBoxes] = useState<PageBoxes | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setError(null);
    setDetail(null);
    setDocument(null);
    const load =
      mode === "document"
        ? api.document(routeId).then((found) => {
            setDocument(found);
            return api.file(found.document.source_file_id).then(setDetail);
          })
        : api.file(routeId).then(setDetail);

    load.catch((caught) =>
      setError(
        caught instanceof ApiError && caught.status === 404
          ? "This document is in a library you can’t see, or it no longer exists."
          : "Could not load this document.",
      ),
    );
  }, [mode, routeId]);

  // Offset between this view's page numbering and the file's.
  const pageStart = document_?.document.page_start ?? 1;
  const fileId = document_?.document.source_file_id ?? routeId;
  const filePage = pageStart + localPage - 1;

  // Boxes are fetched per page: ocr.json for a 300-page bundle is large, and the
  // viewer only ever draws one page.
  useEffect(() => {
    if (!query.trim() || !detail) {
      setBoxes(null);
      return;
    }
    const controller = new AbortController();
    api
      .pageBoxes(fileId, filePage, controller.signal)
      .then(setBoxes)
      .catch(() => setBoxes(null));
    return () => controller.abort();
  }, [fileId, filePage, query, detail]);

  const filePageCount = detail?.pages.length ?? detail?.source_file.page_count ?? 1;
  const pageCount =
    mode === "document" && document_
      ? document_.document.page_end - document_.document.page_start + 1
      : filePageCount;

  const go = useMemo(
    () => (next: number) => {
      const clamped = Math.min(Math.max(next, 1), pageCount);
      const suffix = query ? `?q=${encodeURIComponent(query)}` : "";
      const base = mode === "document" ? `/document/${routeId}` : `/file/${routeId}`;
      navigate(`${base}/page/${clamped}${suffix}`, { replace: true });
    },
    [mode, routeId, navigate, pageCount, query],
  );

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.target instanceof HTMLInputElement) return;
      if (event.key === "ArrowRight" || event.key === "j") go(localPage + 1);
      if (event.key === "ArrowLeft" || event.key === "k") go(localPage - 1);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [go, localPage]);

  if (error) {
    return <div className="mx-auto max-w-2xl px-6 py-20 text-center text-muted">{error}</div>;
  }
  if (!detail) {
    return <div className="mx-auto max-w-2xl px-6 py-20 text-center text-muted">Loading…</div>;
  }

  const isSegment = mode === "document" && pageCount < filePageCount;
  const title =
    document_?.document.title ?? detail.source_file.original_filename ?? "(no filename)";

  return (
    <div className="mx-auto flex max-w-7xl gap-6 px-6 py-6">
      <ThumbnailStrip
        fileId={fileId}
        pages={
          mode === "document" && document_
            ? document_.pages.map((p) => p.page_number)
            : detail.pages.map((p) => p.page_number)
        }
        pageStart={pageStart}
        current={filePage}
        query={query}
        basePath={mode === "document" ? `/document/${routeId}` : `/file/${routeId}`}
      />

      <div className="min-w-0 flex-1">
        <header className="mb-4 flex flex-wrap items-baseline justify-between gap-3">
          <div className="min-w-0">
            <h1 className="flex items-center gap-2 truncate text-lg font-medium">
              {title}
              {document_?.known_form && (
                <span
                  title={document_.known_form.name}
                  className="shrink-0 rounded-full border border-accent/50 px-2 py-0.5 text-xs font-normal text-accent"
                >
                  {document_.known_form.code}
                </span>
              )}
            </h1>
            <p className="text-sm text-muted">
              Page {localPage} of {pageCount}
              {/* Never hide that this is a slice of something larger (REQ-030). */}
              {isSegment && (
                <>
                  {" · page "}
                  {filePage} of {filePageCount} in{" "}
                  <Link
                    to={`/file/${fileId}/page/${filePage}`}
                    className="underline underline-offset-2"
                  >
                    {detail.source_file.original_filename}
                  </Link>
                </>
              )}
              {query && <> · highlighting “{query}”</>}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <NavButton onClick={() => go(localPage - 1)} disabled={localPage <= 1}>
              ← Prev
            </NavButton>
            <NavButton onClick={() => go(localPage + 1)} disabled={localPage >= pageCount}>
              Next →
            </NavButton>
            <Link
              to={`/file/${fileId}/segments`}
              className="rounded-md border border-edge px-3 py-1.5 text-sm text-muted hover:border-accent/60"
            >
              Segments
            </Link>
            <a
              href={
                mode === "document" ? fileUrl.documentPdf(routeId) : fileUrl.pdf(routeId)
              }
              target="_blank"
              rel="noreferrer"
              title={
                mode === "document"
                  ? "Just this document's pages, as a standalone PDF"
                  : "The whole file"
              }
              className="rounded-md border border-edge px-3 py-1.5 text-sm text-muted hover:border-accent/60"
            >
              {mode === "document" && isSegment ? "Export PDF" : "Open PDF"}
            </a>
          </div>
        </header>

        <PageCanvas fileId={fileId} page={filePage} boxes={boxes} query={query} />
      </div>
    </div>
  );
}

function NavButton({
  children,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      {...props}
      className="rounded-md border border-edge px-3 py-1.5 text-sm disabled:opacity-40"
    >
      {children}
    </button>
  );
}

/**
 * The page image with matched words boxed on top.
 *
 * Boxes are positioned as percentages of the page's own coordinate space rather
 * than in pixels, so the overlay stays aligned at any render resolution, on any
 * screen, without ever reading the image's natural size.
 */
function PageCanvas({
  fileId,
  page,
  boxes,
  query,
}: {
  fileId: string;
  page: number;
  boxes: PageBoxes | null;
  query: string;
}) {
  const [failed, setFailed] = useState(false);
  const terms = useMemo(() => queryTerms(query), [query]);

  const highlights = useMemo(() => {
    if (!boxes || terms.length === 0 || !boxes.width || !boxes.height) return [];
    return boxes.lines
      .flatMap((line) => line.words)
      .filter((word) => matchesTerm(word.t, terms))
      .map((word) => ({
        left: (word.x0 / boxes.width) * 100,
        top: (word.y0 / boxes.height) * 100,
        width: ((word.x1 - word.x0) / boxes.width) * 100,
        height: ((word.y1 - word.y0) / boxes.height) * 100,
      }));
  }, [boxes, terms]);

  if (failed) {
    return (
      <div className="rounded-lg border border-edge bg-surface p-10 text-center text-sm text-muted">
        This page hasn’t been rendered yet.{" "}
        <a href={fileUrl.pdf(fileId)} className="text-accent underline underline-offset-2">
          Open the PDF instead
        </a>
        .
      </div>
    );
  }

  return (
    <div className="relative overflow-hidden rounded-lg border border-edge bg-white">
      <img
        key={`${fileId}-${page}`}
        src={fileUrl.render(fileId, page)}
        alt={`Page ${page}`}
        onError={() => setFailed(true)}
        className="block w-full"
      />
      {highlights.map((box, index) => (
        <span
          key={index}
          aria-hidden
          className="pointer-events-none absolute rounded-[2px] bg-amber-400/40 ring-1 ring-amber-500/70"
          style={{
            left: `${box.left}%`,
            top: `${box.top}%`,
            width: `${box.width}%`,
            height: `${box.height}%`,
          }}
        />
      ))}
    </div>
  );
}

function ThumbnailStrip({
  fileId,
  pages,
  pageStart,
  current,
  query,
  basePath,
}: {
  fileId: string;
  pages: number[];
  pageStart: number;
  current: number;
  query: string;
  basePath: string;
}) {
  const active = useRef<HTMLAnchorElement>(null);
  useEffect(() => {
    active.current?.scrollIntoView({ block: "nearest" });
  }, [current]);

  if (pages.length <= 1) return null;
  const suffix = query ? `?q=${encodeURIComponent(query)}` : "";

  return (
    <nav className="hidden max-h-[calc(100vh-6rem)] w-28 shrink-0 overflow-y-auto lg:block">
      <ul className="space-y-2">
        {pages.map((number) => (
          <li key={number}>
            <Link
              ref={number === current ? active : undefined}
              to={`${basePath}/page/${number - pageStart + 1}${suffix}`}
              replace
              className={`block rounded border ${
                number === current ? "border-accent" : "border-edge hover:border-muted"
              }`}
            >
              <img
                src={fileUrl.thumb(fileId, number)}
                alt=""
                loading="lazy"
                className="block w-full rounded-[3px]"
              />
              <span className="block py-1 text-center text-xs text-muted">{number}</span>
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}
