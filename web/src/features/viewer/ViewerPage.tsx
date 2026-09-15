import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { Pencil } from "lucide-react";

import {
  ApiError,
  api,
  type DocumentDetail,
  type PageBoxes,
  type SourceFileDetail,
  fileUrl,
} from "../../api";
import { matchesTerm, queryTerms } from "../../lib/highlight";
import { pageParts } from "../../lib/pages";
import { ErrorState } from "../../components/States";
import { isInteractiveTarget, isTypingTarget, shortcutsEnabled } from "../../lib/keyboard";
import EditPanel from "../edit/EditPanel";
import WhyPanel from "../why/WhyPanel";
import MoveToVault from "../vault/MoveToVault";
import { Button, EmptyState, Link as TextLink, Tooltip } from "@d3cloud/ui";

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
/**
 * Keyed on what it is showing, so navigating from one document to another
 * remounts rather than resetting.
 *
 * The reset used to be an effect — `setError(null); setDetail(null);
 * setDocument(null)` at the top of the loader — which meant one render showing
 * the *previous* document's title above the new one's pages before the state
 * caught up. A `key` makes that impossible rather than brief.
 */
export default function ViewerPage({ mode }: { mode: "document" | "file" }) {
  const params_ = useParams();
  const routeId = (mode === "document" ? params_.documentId : params_.fileId) ?? "";
  return <Viewer mode={mode} routeId={routeId} key={`${mode}:${routeId}`} />;
}

function Viewer({
  mode,
  routeId,
}: {
  mode: "document" | "file";
  routeId: string;
}) {
  const params_ = useParams();
  const pageNumber = params_.pageNumber ?? "1";
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const query = params.get("q") ?? "";
  const localPage = Math.max(1, Number(pageNumber) || 1);

  const [detail, setDetail] = useState<SourceFileDetail | null>(null);
  const [document_, setDocument] = useState<DocumentDetail | null>(null);
  const [fetchedBoxes, setBoxes] = useState<PageBoxes | null>(null);
  // Derived rather than cleared: with no search term there is nothing to
  // highlight, and saying so at render cannot leave the previous page's boxes
  // drawn over this one for a frame.
  const boxes = query.trim() && detail ? fetchedBoxes : null;
  const [error, setError] = useState<unknown>(null);
  // Bumped by the retry button so the load effect runs again.
  const [reloadToken, setReloadToken] = useState(0);
  const [showWhy, setShowWhy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);

  useEffect(() => {
    // No resetting here any more: the `key` above guarantees this component is
    // fresh whenever `routeId` changes, so there is no previous document's
    // state to clear.
    const load =
      mode === "document"
        ? api.document(routeId).then((found) => {
            setDocument(found);
            return api.file(found.document.source_file_id).then(setDetail);
          })
        : api.file(routeId).then(setDetail);

    // The error itself, not a sentence about it. A transient failure and a
    // 404 need different offers — one is worth retrying and the other is not —
    // and flattening both to a string made that impossible to tell apart
    // downstream (D-06).
    load.catch(setError);
  }, [mode, routeId, reloadToken]);

  // Offset between this view's page numbering and the file's.
  const pageStart = document_?.document.page_start ?? 1;
  const fileId = document_?.document.source_file_id ?? routeId;
  const filePage = pageStart + localPage - 1;

  // Boxes are fetched per page: ocr.json for a 300-page bundle is large, and the
  // viewer only ever draws one page.
  useEffect(() => {
    // Nothing to highlight. `boxes` is derived away at render below, so this
    // only has to not fetch.
    if (!query.trim() || !detail) return;
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

  // Every page of the *file* that matches, not just this document's. The whole
  // point of the product is a term buried in a bundle, and the bundle's other
  // occurrences are usually in a different constituent document — so this is
  // deliberately file-scoped and crosses document boundaries (D-03).
  // Stored with the query it belongs to, and read back only when the two
  // agree. Keeping a bare number[] meant the previous query's ticks stayed on
  // screen until the next fetch resolved — pointing at pages that no longer
  // match, which is the one failure a reader would actually notice.
  const [fetchedMatches, setFetchedMatches] = useState<{
    query: string;
    pages: number[];
  }>({ query: "", pages: [] });
  useEffect(() => {
    const term = query.trim();
    if (!term || !fileId) return;
    let live = true;
    api
      .fileMatches(fileId, term)
      .then((r) => live && setFetchedMatches({ query: term, pages: r.pages }))
      // The page is readable without the match set; a failed fetch must not
      // replace the document with an error.
      .catch(() => live && setFetchedMatches({ query: term, pages: [] }));
    return () => {
      live = false;
    };
  }, [fileId, query]);
  const matches =
    query.trim() && fetchedMatches.query === query.trim() ? fetchedMatches.pages : [];

  // Where the current page sits in that set. -1 when the page itself does not
  // match, which is the ordinary state after stepping with Prev/Next.
  const matchIndex = matches.indexOf(filePage);
  const nextMatch = matches.find((p) => p > filePage);
  const previousMatch = [...matches].reverse().find((p) => p < filePage);

  const goToFilePage = useMemo(
    () => (page: number) => {
      const suffix = query ? `?q=${encodeURIComponent(query)}` : "";
      // Always by file page: a match in another constituent document cannot be
      // addressed in this document's coordinates, which is exactly why Prev
      // and Next could never reach it.
      navigate(`/file/${fileId}/page/${page}${suffix}`, { replace: true });
    },
    [fileId, navigate, query],
  );

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
      if (!shortcutsEnabled()) return;
      if (isTypingTarget(event.target)) return;
      // The arrows still work on a focused control; only the bare letters are
      // withheld, because those are the ones a control may want for itself.
      if (isInteractiveTarget(event.target) && event.key.length === 1) return;
      if (event.key === "ArrowRight" || event.key === "j") go(localPage + 1);
      if (event.key === "ArrowLeft" || event.key === "k") go(localPage - 1);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [go, localPage]);

  if (error) {
    // A 404 here is deliberately ambiguous and must stay that way: the API
    // answers 404 rather than 403 for a library you are not in, because a 403
    // would confirm the document exists (ADR-005). So this does not claim
    // "not yours" — it says what is true, and gives a way onward, which is
    // what the single grey sentence never did.
    const missing = error instanceof ApiError && error.status === 404;
    return (
      <div className="mx-auto max-w-2xl py-12">
        {missing ? (
          /* kind="no-access" for both a missing and an unreadable file. The API
             answers 404 either way on purpose (ADR-005), and "you cannot see
             this" is the one statement true in both cases — the attribute says
             nothing the page does not already render identically. */
          <EmptyState kind="no-access" heading="Not here">
            <p>
              This {mode === "document" ? "document" : "file"} either does not
              exist or is in a library you are not a member of. Bindery cannot
              tell you which — saying so would confirm whether it exists.
            </p>
            <p className="flex flex-wrap gap-3">
              <Link
                to="/search"
                className="rounded-md border border-field px-3 py-1.5 text-sm text-fg"
              >
                Search the archive
              </Link>
              <TextLink asChild variant="inline">
                <Link to="/libraries" className="self-center text-sm">
                  Check which libraries you are in
                </Link>
              </TextLink>
            </p>
          </EmptyState>
        ) : (
          <ErrorState error={error} onRetry={() => {
              // Clear the error too, or the branch keeps rendering
              // over a load that has already succeeded.
              setError(null);
              setReloadToken((n) => n + 1);
            }} />
        )}
      </div>
    );
  }
  if (!detail) {
    return <div className="mx-auto max-w-2xl py-12 text-center text-muted">Loading…</div>;
  }

  const isSegment = mode === "document" && pageCount < filePageCount;
  const title =
    document_?.document.title ?? detail.source_file.original_filename ?? "(no filename)";

  return (
    <div className="mx-auto flex max-w-7xl gap-6">
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
                <abbr
                  title={document_.known_form.name}
                  className="shrink-0 rounded-full border border-accent/50 px-2 py-0.5 text-xs font-normal text-accent no-underline"
                >
                  {document_.known_form.code}
                </abbr>
              )}
            </h1>
            <p className="text-sm text-muted">
              {/* Never hide that this is a slice of something larger (REQ-030).
                  Split at the filename so it can stay a link, but worded by the
                  same helper the palette and search use — this header used to
                  state the rule in its own grammar (D-04). */}
              {pageParts({
                documentPage: localPage,
                documentPageCount: pageCount,
                filePage,
                filePageCount,
                filename: isSegment ? detail.source_file.original_filename : null,
              }).lead}
              {isSegment && (
                <>
                  {" "}
                  {/* Carry the query across the mode change. Every sibling
                      link in this file appends the suffix and this one did
                      not, so stepping up from a document to its file threw
                      away the highlighting and the match set (D-03). */}
                  <TextLink asChild variant="inline">
                    <Link
                      to={`/file/${fileId}/page/${filePage}${
                        query ? `?q=${encodeURIComponent(query)}` : ""
                      }`}
                    >
                      {detail.source_file.original_filename}
                    </Link>
                  </TextLink>
                </>
              )}
              {query && <> · highlighting “{query}”</>}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {/* Kept visually apart from Prev/Next, and labelled, because they
                step different things: Prev/Next walk pages of what you are
                reading, this walks occurrences of what you searched for —
                across document boundaries, inside one file. Search could count
                these and nothing could reach them (D-03). */}
            {query && matches.length > 0 && (
              <div className="mr-1 flex items-center gap-1.5 rounded-md border border-field px-2 py-1">
                <span className="text-xs text-muted">
                  {matchIndex >= 0
                    ? `Match ${matchIndex + 1} of ${matches.length}`
                    : `${matches.length} match${matches.length === 1 ? "" : "es"}`}
                </span>
                <Button variant="ghost" onClick={() => previousMatch && goToFilePage(previousMatch)} disabled={previousMatch === undefined} aria-label="Previous match">
                  ↑
                </Button>
                <Button variant="ghost" onClick={() => nextMatch && goToFilePage(nextMatch)} disabled={nextMatch === undefined} aria-label="Next match">
                  ↓
                </Button>
              </div>
            )}
            <NavButton onClick={() => go(localPage - 1)} disabled={localPage <= 1}>
              ← Prev
            </NavButton>
            <NavButton onClick={() => go(localPage + 1)} disabled={localPage >= pageCount}>
              Next →
            </NavButton>
            {mode === "document" && (
              <Button aria-pressed={showWhy} onClick={() => setShowWhy((open) => !open)}>
                Why?
              </Button>
            )}
            {mode === "document" && (
              <Button
                pressed={editing}
                icon={<Pencil size={14} />}
                onClick={() => setEditing((open) => !open)}
              >
                Edit
              </Button>
            )}
            <Link
              to={`/file/${fileId}/segments`}
              className="rounded-md border border-edge px-3 py-1.5 text-sm text-muted hover:border-accent/60"
            >
              Segments
            </Link>
            {/* Documents only: the vault holds a document, not a whole bundle,
                because a bundle is usually one vaultable page among fifty. */}
            {mode === "document" && <MoveToVault documentId={routeId} title={title} />}
            <Tooltip
              content={
                mode === "document"
                  ? "Just this document's pages, as a standalone PDF"
                  : "The whole file"
              }
            >
              <a
                href={
                  mode === "document" ? fileUrl.documentPdf(routeId) : fileUrl.pdf(routeId)
                }
                target="_blank"
                rel="noreferrer"
                className="rounded-md border border-edge px-3 py-1.5 text-sm text-muted hover:border-accent/60"
              >
                {mode === "document" && isSegment ? "Export PDF" : "Open PDF"}
              </a>
            </Tooltip>
          </div>
        </header>

        {/* Always in the DOM, `sr-only` while empty — a live region inserted
            at the same moment as its text is not reliably announced, and this
            message is the list of fields the edit actually changed. */}
        <p
          role="status"
          aria-live="polite"
          className={
            saved
              ? "mb-3 rounded-lg border border-accent/40 bg-accent/5 px-3 py-2 text-sm text-accent"
              : "sr-only"
          }
        >
          {saved}
        </p>

        {editing && document_ && (
          <div className="mb-4">
            <EditPanel
              detail={document_}
              onCancel={() => setEditing(false)}
              onSaved={(message) => {
                setSaved(message);
                setEditing(false);
                // Re-read rather than patching local state: the server decides
                // what actually changed, and the field badges have to come back
                // from it or they will claim a source the row does not have.
                void api.document(routeId).then(setDocument);
              }}
            />
          </div>
        )}

        <div className={showWhy ? "grid gap-6 xl:grid-cols-[1fr_22rem]" : ""}>
          <PageCanvas fileId={fileId} page={filePage} boxes={boxes} query={query} />
          {showWhy && mode === "document" && (
            <WhyPanel
              documentId={routeId}
              fileId={fileId}
              onClose={() => setShowWhy(false)}
            />
          )}
        </div>
      </div>
    </div>
  );
}

function NavButton({
  children,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <Button {...props}>
      {children}
    </Button>
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
        <TextLink href={fileUrl.pdf(fileId)} variant="inline">
          Open the PDF instead
        </TextLink>
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
          /* On the white of a scanned page. Amber was 1.26:1 here — the one
             mark that proves the product found the word, and it was all but
             invisible. The 2px ring is 4.2:1 on paper; the wash stays light
             so the printed word underneath is still readable. */
          /* d3-allow: a highlight drawn over a line of scanned text, sized to the word rather than to a UI surface. The radius scale starts at 6px because that is where a control reads as a control; a 6px corner on a box two lines high would round the word away. */
          className="pointer-events-none absolute rounded-[2px] bg-mark/15 ring-2 ring-mark"
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
                /* d3-allow: a highlight drawn over a line of scanned text, sized to the word rather than to a UI surface. The radius scale starts at 6px because that is where a control reads as a control; a 6px corner on a box two lines high would round the word away. */
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
