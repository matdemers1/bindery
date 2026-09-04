import PageImage from "../../components/PageThumb";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router";

import { ApiError, api, type SegmentList, type SourceFileDetail } from "../../api";
import { Empty, ErrorState } from "../../components/States";

/**
 * The manual segmentation editor (REQ-036).
 *
 * Built before the heuristics, deliberately: it is the ground truth automation
 * is measured against, and it means automation is never load-bearing. A bundle
 * the proposers cut badly is always one click from being cut correctly.
 *
 * Splits are toggled at the seam between two pages rather than dragged. A drag
 * has to be aimed; a seam is a discrete thing you either want or you don't, and
 * on a 100-page bundle precision beats gesture.
 */
export default function SegmentationPage() {
  const { fileId = "" } = useParams();

  const [detail, setDetail] = useState<SourceFileDetail | null>(null);
  const [saved, setSaved] = useState<SegmentList | null>(null);
  const [splits, setSplits] = useState<number[]>([]);
  const [titles, setTitles] = useState<Record<number, string>>({});
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  // Bumped by the retry button so the load effect runs again.
  const [reloadToken, setReloadToken] = useState(0);

  const adopt = useCallback((list: SegmentList) => {
    setSaved(list);
    setSplits(list.segments.slice(1).map((segment) => segment.page_start));
    setTitles(
      Object.fromEntries(
        list.segments.map((segment) => [segment.page_start, segment.title ?? ""]),
      ),
    );
  }, []);

  // Adjusted during render: a stale error from the previous file must not sit
  // above the new one while it loads.
  const [loadedFor, setLoadedFor] = useState(fileId);
  if (fileId !== loadedFor) {
    setLoadedFor(fileId);
    setError(null);
  }

  useEffect(() => {
    Promise.all([api.file(fileId), api.segments(fileId)])
      .then(([file, list]) => {
        setDetail(file);
        adopt(list);
      })
      // The error itself, not a sentence about it — a transient failure is
      // worth retrying and a 404 is not, and one string could not say which
      // (D-06).
      .catch(setError);
  }, [fileId, adopt, reloadToken]);

  const pageCount = detail?.pages.length ?? 0;

  // Splits are the source of truth in the editor; segments are derived, so the
  // set is always a gapless cover by construction and the API's validation
  // never has to reject what the UI produced.
  const segments = useMemo(() => {
    const starts = [1, ...splits];
    const ends = [...splits.map((page) => page - 1), pageCount];
    return starts.map((start, index) => ({ start, end: ends[index] }));
  }, [splits, pageCount]);

  const dirty = useMemo(() => {
    if (!saved) return false;
    const savedStarts = saved.segments.map((segment) => segment.page_start).join(",");
    const savedTitles = saved.segments
      .map((segment) => `${segment.page_start}:${segment.title ?? ""}`)
      .join("|");
    const nextTitles = segments
      .map(({ start }) => `${start}:${titles[start] ?? ""}`)
      .join("|");
    return (
      savedStarts !== segments.map(({ start }) => start).join(",") ||
      savedTitles !== nextTitles
    );
  }, [saved, segments, titles]);

  function toggleSplit(page: number) {
    setSplits((current) =>
      current.includes(page)
        ? current.filter((value) => value !== page)
        : [...current, page].sort((a, b) => a - b),
    );
  }

  async function save() {
    setBusy(true);
    setStatus(null);
    try {
      const list = await api.replaceSegments(
        fileId,
        segments.map(({ start, end }) => ({
          page_start: start,
          page_end: end,
          title: titles[start]?.trim() || null,
        })),
      );
      adopt(list);
      const matched = list.segments.filter((segment) => segment.known_form_id).length;
      setStatus(
        `Saved ${list.segments.length} ${list.segments.length === 1 ? "document" : "documents"}` +
          (matched ? ` · ${matched} matched a known form` : ""),
      );
    } catch (caught) {
      setStatus(caught instanceof ApiError ? caught.message : "Could not save.");
    } finally {
      setBusy(false);
    }
  }

  async function undo() {
    setBusy(true);
    setStatus(null);
    try {
      adopt(await api.undoSegments(fileId));
      setStatus("Reverted to the previous segmentation.");
    } catch (caught) {
      setStatus(
        caught instanceof ApiError && caught.status === 409
          ? "There’s no earlier segmentation to return to."
          : "Could not undo.",
      );
    } finally {
      setBusy(false);
    }
  }

  if (error) {
    // Deliberately not "not yours": the API answers 404 rather than 403 for a
    // library you are not in, because a 403 would confirm the file exists
    // (ADR-005). The ambiguity is the boundary working; what was missing was
    // any way onward from it.
    const missing = error instanceof ApiError && error.status === 404;
    return (
      <div className="mx-auto max-w-2xl py-12">
        {missing ? (
          <Empty title="Not here">
            <p>
              This file either does not exist or is in a library you are not a
              member of. Bindery cannot tell you which — saying so would confirm
              whether it exists.
            </p>
            <p className="mt-3 flex flex-wrap gap-3">
              <Link
                to="/files"
                className="rounded-md border border-field px-3 py-1.5 text-sm text-neutral-100"
              >
                Browse files
              </Link>
              <Link
                to="/libraries"
                className="self-center text-sm text-accent underline underline-offset-2"
              >
                Check which libraries you are in
              </Link>
            </p>
          </Empty>
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
  if (!detail || !saved) {
    return <div className="mx-auto max-w-2xl py-12 text-center text-muted">Loading…</div>;
  }

  return (
    <div className="mx-auto max-w-6xl">
      <header className="mb-6 flex flex-wrap items-baseline justify-between gap-3">
        <div className="min-w-0">
          <h1 className="truncate text-lg font-medium">
            {detail.source_file.original_filename ?? "(no filename)"}
          </h1>
          <p className="text-sm text-muted">
            {pageCount} pages · {segments.length}{" "}
            {segments.length === 1 ? "document" : "documents"} · the original file is
            never modified
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={undo}
            disabled={busy}
            className="rounded-md border border-field px-3 py-1.5 text-sm disabled:opacity-50"
          >
            Undo last save
          </button>
          <button
            onClick={save}
            disabled={busy || !dirty}
            /* "Saved" is the resting state of this button, so it is on screen
               far more than "Save segmentation" is — and `opacity-40` faded the
               whole group, label included, to 2.3:1. Disabled should read as
               "nothing to do", not as "unreadable". */
            className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-ink disabled:bg-accent/70"
          >
            {busy ? "Saving…" : dirty ? "Save segmentation" : "Saved"}
          </button>
        </div>
      </header>

      {/* Always in the DOM, `sr-only` while empty — a live region inserted at
          the same moment as its text is not reliably announced. */}
      <p
        role="status"
        aria-live="polite"
        className={status ? "mb-4 text-sm text-accent" : "sr-only"}
      >
        {status}
      </p>

      <div className="space-y-6">
        {segments.map((segment, index) => (
          <section
            key={segment.start}
            /* `field`, not `edge`: this border is not a divider, it is the
               boundary of the one thing this screen is about — where a
               document starts and stops inside the file. At 1.35:1 the
               grouping that carries the whole meaning was invisible. */
            className="rounded-lg border border-field bg-surface p-4"
          >
            <div className="mb-3 flex flex-wrap items-center gap-3">
              <span className="rounded-full border border-edge px-2 py-0.5 font-mono text-xs text-muted">
                {segment.start === segment.end
                  ? `page ${segment.start}`
                  : `pages ${segment.start}–${segment.end}`}
              </span>
              <input
                aria-label={
                  segment.start === segment.end
                    ? `Title for the document on page ${segment.start}`
                    : `Title for the document on pages ${segment.start} to ${segment.end}`
                }
                value={titles[segment.start] ?? ""}
                onChange={(event) =>
                  setTitles((current) => ({ ...current, [segment.start]: event.target.value }))
                }
                placeholder={`Document ${index + 1} — untitled`}
                className="min-w-0 flex-1 rounded-md border border-field bg-ink px-3 py-1.5 text-sm outline-none focus:border-accent"
              />
              {saved.segments.find(
                (existing) => existing.page_start === segment.start && existing.known_form_id,
              ) && (
                <span className="shrink-0 rounded-full border border-accent/50 px-2 py-0.5 text-xs text-accent">
                  known form
                </span>
              )}
            </div>

            <ol className="flex flex-wrap gap-1">
              {Array.from(
                { length: segment.end - segment.start + 1 },
                (_, offset) => segment.start + offset,
              ).map((page) => (
                <li key={page} className="flex items-stretch">
                  <PageThumb fileId={fileId} page={page} />
                  {page < pageCount && (
                    <SplitHandle
                      active={splits.includes(page + 1)}
                      onClick={() => toggleSplit(page + 1)}
                      page={page}
                    />
                  )}
                </li>
              ))}
            </ol>
          </section>
        ))}
      </div>

      <p className="mt-8 text-xs text-muted">
        Click a seam to start a new document there. Nothing is written until you save,
        and every save can be undone —{" "}
        <Link to={`/file/${fileId}/page/1`} className="underline underline-offset-2">
          view the whole file
        </Link>
        .
      </p>
    </div>
  );
}

function PageThumb({ fileId, page }: { fileId: string; page: number }) {
  return (
    <figure className="w-20">
      <PageImage
        sourceFileId={fileId}
        page={page}
        className="block aspect-[3/4] w-full rounded border border-edge object-cover object-top"
      />
      <figcaption className="pt-0.5 text-center font-mono text-[10px] text-muted">
        {page}
      </figcaption>
    </figure>
  );
}

function SplitHandle({
  active,
  onClick,
  page,
}: {
  active: boolean;
  onClick: () => void;
  page: number;
}) {
  const label = active
    ? `Remove the split before page ${page + 1}`
    : `Start a new document at page ${page + 1}`;
  // The rail stays 8px wide; the button around it is 24, which is what SC
  // 2.5.8 asks for and what makes it hittable with a tremor or a thumb. The
  // resting fill is `field` (3.3:1 on surface) rather than transparent,
  // because a control that cannot be seen until it is hovered cannot be found
  // at all by someone who is not using a mouse — and `title` never appears on
  // touch, so it was also the only name this had.
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      aria-label={label}
      title={label}
      className="group flex w-6 shrink-0 justify-center self-stretch"
    >
      <span
        aria-hidden
        className={`w-2 rounded-full transition-colors ${
          active ? "bg-accent" : "bg-field group-hover:bg-muted"
        }`}
      />
    </button>
  );
}
