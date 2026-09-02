import PageThumb from "../../components/PageThumb";
import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { Library as LibraryIcon } from "lucide-react";

import { ApiError, api, type Archive, type ArchiveEntry, type BulkResult, type Tree } from "../../api";
import { SourceChip } from "../why/WhyPanel";

/**
 * The archive browser (screen 2).
 *
 * Search answers "where is the thing I know I have". This answers the other
 * question — what is in here, how did it get here, and what did the system
 * decide about it — which previously had no answer: an empty search showed
 * nothing, so a document you couldn't name was a document you couldn't reach.
 *
 * Provenance is a first-class column, not a detail view: "what came off the
 * scanner last week and what did it get tagged" should be one screen.
 */
const INGEST_LABELS: Record<string, string> = {
  watched_folder: "scanner",
  web_upload: "uploaded",
  camera: "camera",
  bulk_import: "imported",
};

const GROUPINGS = [
  { key: "year", label: "Year" },
  { key: "correspondent", label: "From" },
  { key: "type", label: "Type" },
  { key: "form", label: "Known form" },
];

export default function ArchivePage() {
  const [params, setParams] = useSearchParams();
  // Computed once rather than inside the dependency list. `params.toString()`
  // in a dep array is a fresh value the linter cannot reason about, and it is
  // recomputed on every render for a comparison that only needs the string.
  const search = params.toString();
  const [archive, setArchive] = useState<Archive | null>(null);
  const [tree, setTree] = useState<Tree | null>(null);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [lastOperation, setLastOperation] = useState<BulkResult | null>(null);
  const [bulkNotice, setBulkNotice] = useState<string | null>(null);

  const groupBy = params.get("group") ?? "year";
  const filters = {
    q: params.get("q") ?? "",
    year: params.get("year") ?? "",
    correspondent: params.getAll("correspondent"),
    document_type: params.getAll("document_type"),
    tag: params.getAll("tag"),
    sort: params.get("sort") ?? "newest",
  };

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [entries, groups] = await Promise.all([
        api.archive(filters as never),
        api.tree(groupBy),
      ]);
      setArchive(entries);
      setTree(groups);
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, groupBy]);

  useEffect(() => {
    // An async data load: the state is genuinely unavailable on the first
    // render, so the extra pass is the point rather than a mistake.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  function update(mutate: (next: URLSearchParams) => void) {
    const next = new URLSearchParams(params);
    mutate(next);
    setParams(next, { replace: true });
  }

  const stats = archive?.stats;
  const active =
    filters.q || filters.year || filters.correspondent.length || filters.tag.length ||
    filters.document_type.length;

  return (
    <div className="mx-auto max-w-7xl">
      <header className="mb-5">
        <h1 className="flex items-center gap-2.5 text-xl font-semibold tracking-tight">
          <LibraryIcon size={19} className="text-accent" />
          Archive
        </h1>
        {stats && (
          <p className="mt-1 text-sm text-muted">
            {stats.documents} {stats.documents === 1 ? "document" : "documents"} across{" "}
            {stats.files} {stats.files === 1 ? "file" : "files"} · {stats.pages} pages
            {stats.needs_review > 0 && (
              <>
                {" · "}
                <Link to="/review" className="text-accent underline underline-offset-2">
                  {stats.needs_review} awaiting review
                </Link>
              </>
            )}
            {/* A separate count, because it is a separate queue. Adding the two
                together and linking the total to /review is what produced a
                header advertising 408 documents and a review screen with
                nothing on it. */}
            {stats.backlog_pending > 0 && (
              <>
                {" · "}
                <Link to="/pipeline" className="underline underline-offset-2">
                  {stats.backlog_pending} imported, unreviewed
                </Link>
              </>
            )}
            {stats.unclassified > 0 && (
              <>
                {" · "}
                {/* A link now that there is somewhere for it to go. It was a
                    dead end next to a live one, which reads as "nothing you
                    can do about this". */}
                <Link to="/pipeline" className="text-accent underline underline-offset-2">
                  {stats.unclassified} not yet classified
                </Link>
              </>
            )}
          </p>
        )}
      </header>

      <div className="grid gap-6 lg:grid-cols-[15rem_1fr]">
        <aside>
          <div className="mb-4 flex flex-wrap gap-1">
            {GROUPINGS.map((option) => (
              <button
                key={option.key}
                onClick={() => update((next) => next.set("group", option.key))}
                className={`rounded-md px-2 py-1 text-xs ${
                  groupBy === option.key
                    ? "bg-surface text-neutral-100"
                    : "text-muted hover:text-neutral-200"
                }`}
              >
                {option.label}
              </button>
            ))}
          </div>

          <ul className="space-y-0.5">
            {tree?.groups.map((group) => {
              const key = groupBy === "year" ? "year" : groupBy === "correspondent"
                ? "correspondent" : "document_type";
              const selected =
                groupBy === "year"
                  ? filters.year === group.label
                  : params.getAll(key).includes(group.label);
              return (
                <li key={group.label}>
                  <button
                    onClick={() =>
                      update((next) => {
                        if (groupBy === "form") return;
                        if (selected) next.delete(key);
                        else next.set(key, group.label);
                      })
                    }
                    className={`flex w-full items-baseline justify-between gap-2 rounded px-2 py-1 text-left text-sm ${
                      selected ? "bg-accent/15 text-accent" : "text-neutral-300 hover:bg-surface"
                    }`}
                  >
                    <span className="truncate">{group.label}</span>
                    <span className="shrink-0 text-xs text-muted">{group.count}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        </aside>

        <div className="min-w-0">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <label htmlFor="archive-filter" className="sr-only">
              Filter by title or filename
            </label>
            <input
              id="archive-filter"
              defaultValue={filters.q}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  const value = (event.target as HTMLInputElement).value;
                  update((next) => (value ? next.set("q", value) : next.delete("q")));
                }
              }}
              placeholder="Filter by title or filename…"
              className="min-w-0 flex-1 rounded-md border border-field bg-surface px-3 py-1.5 text-sm outline-none focus:border-accent"
            />
            <select
              aria-label="Sort order"
              value={filters.sort}
              onChange={(event) => update((next) => next.set("sort", event.target.value))}
              className="rounded-md border border-field bg-surface px-2 py-1.5 text-sm"
            >
              <option value="newest">Newest in</option>
              <option value="oldest">Oldest in</option>
              <option value="date">Document date</option>
              <option value="title">Title</option>
            </select>
            {active && (
              <button
                onClick={() => setParams(new URLSearchParams({ group: groupBy }), { replace: true })}
                className="rounded-md border border-edge px-2 py-1.5 text-sm text-muted"
              >
                Clear
              </button>
            )}
          </div>

          {selected.size > 0 && (
            <BulkBar
              selected={selected}
              onDone={async (result, message) => {
                setLastOperation(result);
                setBulkNotice(message);
                setSelected(new Set());
                await load();
              }}
              onClear={() => setSelected(new Set())}
            />
          )}

          {bulkNotice && (
            <p className="mb-3 flex items-center gap-3 text-sm text-accent">
              {bulkNotice}
              {lastOperation?.operation_id && (
                <button
                  onClick={async () => {
                    await api.bulkUndo(lastOperation.operation_id!);
                    setLastOperation(null);
                    setBulkNotice("Undone.");
                    await load();
                  }}
                  className="rounded border border-edge px-2 py-0.5 text-xs text-muted"
                >
                  Undo
                </button>
              )}
            </p>
          )}

          {loading && !archive ? (
            <ul className="space-y-2">
              {[0, 1, 2, 3].map((n) => (
                <li key={n} className="h-20 animate-pulse rounded-lg border border-edge bg-surface" />
              ))}
            </ul>
          ) : archive && archive.entries.length === 0 ? (
            <Empty filtered={Boolean(active)} />
          ) : (
            <>
              <p className="mb-2 text-xs text-muted">
                Showing {archive?.entries.length} of {archive?.total}
              </p>
              <ul className="space-y-2">
                {archive?.entries.map((entry) => (
                  <Row
                    key={entry.document_id}
                    entry={entry}
                    selected={selected.has(entry.document_id)}
                    onToggle={() =>
                      setSelected((current) => {
                        const next = new Set(current);
                        if (next.has(entry.document_id)) {
                          next.delete(entry.document_id);
                        } else {
                          next.add(entry.document_id);
                        }
                        return next;
                      })
                    }
                  />
                ))}
              </ul>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function Row({
  entry,
  selected,
  onToggle,
}: {
  entry: ArchiveEntry;
  selected: boolean;
  onToggle: () => void;
}) {
  const isSegment =
    entry.file_page_count !== null &&
    entry.page_end - entry.page_start + 1 < entry.file_page_count;

  return (
    <li
      className={`flex gap-3 rounded-lg border bg-surface p-3 ${
        selected ? "border-accent" : "border-edge hover:border-accent/60"
      }`}
    >
      <input
        type="checkbox"
        checked={selected}
        onChange={onToggle}
        aria-label={`Select ${entry.title ?? entry.original_filename ?? "document"}`}
        className="mt-1 shrink-0 accent-amber-400"
      />
      <Link to={`/document/${entry.document_id}/page/1`} className="flex min-w-0 flex-1 gap-3">
        <PageThumb
          sourceFileId={entry.source_file_id}
          page={entry.page_start}
          alt=""
          className="h-20 w-14 shrink-0 rounded border border-edge object-cover object-top"
        />
        <div className="min-w-0 flex-1">
          <p className="flex flex-wrap items-center gap-2">
            <span className="truncate font-medium">
              {entry.title ?? entry.original_filename ?? "(untitled)"}
            </span>
            {entry.known_form && (
              <span className="shrink-0 rounded-full border border-accent/50 px-2 text-xs text-accent">
                {entry.known_form}
              </span>
            )}
            {entry.review_state === "needs_review" && (
              <span className="shrink-0 rounded-full border border-amber-500/50 px-2 text-xs text-amber-300">
                needs review
              </span>
            )}
            {entry.review_state === "pending_classification" && (
              <span className="shrink-0 rounded-full border border-edge px-2 text-xs text-muted">
                unclassified
              </span>
            )}
          </p>

          <p className="mt-0.5 text-xs text-muted">
            {/* Provenance, first class: how it got in and when. */}
            {INGEST_LABELS[entry.ingest_source] ?? entry.ingest_source}
            {" · "}
            {new Date(entry.received_at).toLocaleDateString()}
            {entry.document_date && ` · dated ${entry.document_date}`}
            {entry.correspondent && ` · from ${entry.correspondent}`}
            {entry.document_type && ` · ${entry.document_type}`}
            {isSegment &&
              ` · pages ${entry.page_start}–${entry.page_end} of ${entry.original_filename}`}
          </p>

          {entry.tags.length > 0 && (
            <ul className="mt-1.5 flex flex-wrap gap-1">
              {entry.tags.map((tag) => (
                <li key={tag.id}>
                  <SourceChip source={tag.source}>{tag.name}</SourceChip>
                </li>
              ))}
            </ul>
          )}
        </div>
      </Link>
    </li>
  );
}

/**
 * Bulk edit (T-4.6). The preview is the apply path with writes off, so what it
 * shows is what happens — and the whole operation undoes as one action, because
 * undoing a thousand documents one at a time is the same as no undo.
 */
function BulkBar({
  selected,
  onDone,
  onClear,
}: {
  selected: Set<string>;
  onDone: (result: BulkResult, message: string) => void;
  onClear: () => void;
}) {
  const [tags, setTags] = useState("");
  const [preview, setPreview] = useState<BulkResult | null>(null);
  const [busy, setBusy] = useState(false);

  const actions = () => ({
    add_tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
  });

  return (
    <div className="mb-3 rounded-lg border border-accent/40 bg-surface p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm">
          {selected.size} selected
        </span>
        <input
          aria-label="Tags to add, comma separated"
          value={tags}
          onChange={(event) => {
            setTags(event.target.value);
            setPreview(null);
          }}
          placeholder="Add tags, comma separated"
          className="min-w-0 flex-1 rounded-md border border-field bg-ink px-3 py-1.5 text-sm outline-none focus:border-accent"
        />
        <button
          onClick={async () => {
            setBusy(true);
            try {
              setPreview(await api.bulkPreview([...selected], actions()));
            } finally {
              setBusy(false);
            }
          }}
          disabled={busy || !tags.trim()}
          className="rounded-md border border-edge px-3 py-1.5 text-sm disabled:opacity-40"
        >
          Preview
        </button>
        <button
          onClick={async () => {
            setBusy(true);
            try {
              const result = await api.bulkApply([...selected], actions());
              onDone(result, `Tagged ${result.matched} documents.`);
            } catch (error) {
              onDone(
                { matched: 0, operation_id: null, changes: [] },
                error instanceof ApiError ? error.message : "That didn't work.",
              );
            } finally {
              setBusy(false);
            }
          }}
          disabled={busy || !preview}
          title={preview ? undefined : "Preview it first"}
          className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-ink disabled:opacity-40"
        >
          Apply
        </button>
        <button onClick={onClear} className="text-sm text-muted hover:underline">
          Clear
        </button>
      </div>

      {preview && (
        <p className="mt-2 text-xs text-muted">
          Would change {preview.matched} of {selected.size} selected. Nothing has been
          written.
        </p>
      )}
    </div>
  );
}

function Empty({ filtered }: { filtered: boolean }) {
  return (
    <div className="rounded-lg border border-edge p-10 text-center">
      {filtered ? (
        <p className="text-muted">Nothing matches those filters.</p>
      ) : (
        <>
          <p className="text-lg">Nothing here yet.</p>
          <p className="mt-2 text-sm text-muted">
            Drop files anywhere on the page, or scan into the watched folder. They
            appear here once they've been OCR'd.
          </p>
        </>
      )}
    </div>
  );
}
