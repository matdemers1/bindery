import PageImage from "../../components/PageThumb";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router";

import { ApiError, api, type SegmentList, type SourceFileDetail } from "../../api";

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
  const [error, setError] = useState<string | null>(null);

  const adopt = useCallback((list: SegmentList) => {
    setSaved(list);
    setSplits(list.segments.slice(1).map((segment) => segment.page_start));
    setTitles(
      Object.fromEntries(
        list.segments.map((segment) => [segment.page_start, segment.title ?? ""]),
      ),
    );
  }, []);

  useEffect(() => {
    setError(null);
    Promise.all([api.file(fileId), api.segments(fileId)])
      .then(([file, list]) => {
        setDetail(file);
        adopt(list);
      })
      .catch((caught) =>
        setError(
          caught instanceof ApiError && caught.status === 404
            ? "This file is in a library you can’t see, or it no longer exists."
            : "Could not load this file.",
        ),
      );
  }, [fileId, adopt]);

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
    return <div className="mx-auto max-w-2xl py-12 text-center text-muted">{error}</div>;
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
            className="rounded-md border border-edge px-3 py-1.5 text-sm disabled:opacity-40"
          >
            Undo last save
          </button>
          <button
            onClick={save}
            disabled={busy || !dirty}
            className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-ink disabled:opacity-40"
          >
            {busy ? "Saving…" : dirty ? "Save segmentation" : "Saved"}
          </button>
        </div>
      </header>

      {status && <p className="mb-4 text-sm text-accent">{status}</p>}

      <div className="space-y-6">
        {segments.map((segment, index) => (
          <section
            key={segment.start}
            className="rounded-lg border border-edge bg-surface p-4"
          >
            <div className="mb-3 flex flex-wrap items-center gap-3">
              <span className="rounded-full border border-edge px-2 py-0.5 font-mono text-xs text-muted">
                {segment.start === segment.end
                  ? `page ${segment.start}`
                  : `pages ${segment.start}–${segment.end}`}
              </span>
              <input
                value={titles[segment.start] ?? ""}
                onChange={(event) =>
                  setTitles((current) => ({ ...current, [segment.start]: event.target.value }))
                }
                placeholder={`Document ${index + 1} — untitled`}
                className="min-w-0 flex-1 rounded-md border border-edge bg-ink px-3 py-1.5 text-sm outline-none focus:border-accent"
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
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      title={
        active
          ? `Remove the split before page ${page + 1}`
          : `Start a new document at page ${page + 1}`
      }
      className={`mx-0.5 w-2 shrink-0 self-stretch rounded-full transition-colors ${
        active ? "bg-accent" : "bg-transparent hover:bg-muted/40"
      }`}
    />
  );
}
