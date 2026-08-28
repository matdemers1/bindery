import { useLiveQuery } from "../../live/LiveProvider";
import { Import as ImportIcon } from "lucide-react";

import PageHeader from "../../components/PageHeader";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router";

import { ApiError, api, type ImportItem, type ImportSession, type Library } from "../../api";

/**
 * Backlog import (T-4.1 to T-4.5).
 *
 * Staged on purpose: scan → review → sample → curate → import. Nothing is
 * processed before you have seen how much there is, how much of it is already
 * here, and what it will cost.
 *
 * The risk this whole screen exists to manage is R-03 — that importing five
 * thousand documents floods the daily review queue and the project gets
 * abandoned. Imported documents are flagged and kept out of that queue, and the
 * screen says so rather than leaving you to discover it.
 */
/** How many items are still waiting, from whatever the session reports. */
function remainingOf(session: ImportSession): number {
  const progress = session.progress ?? {};
  return (progress.pending ?? 0) + (progress.sampled ?? 0);
}


export default function ImportPage({ libraries }: { libraries: Library[] }) {
  const [sessions, setSessions] = useState<ImportSession[]>([]);
  const [active, setActive] = useState<ImportSession | null>(null);
  const [failures, setFailures] = useState<ImportItem[]>([]);
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [runningAll, setRunningAll] = useState(false);
  // A ref, not state: the loop reads it between slices and must see the change
  // immediately rather than on the next render.
  const stopRef = useRef(false);

  const load = useCallback(async () => {
    const all = await api.imports();
    setSessions(all);
    setActive((current) => (current ? all.find((s) => s.id === current.id) ?? null : all[0] ?? null));
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Refreshed when files actually move rather than on a timer. The timer
  // version depended on `active`, and replacing `active` with a fresh object
  // every tick tore the interval down and rebuilt it on every response.
  const activeId = active?.id;
  const moving = Boolean(active && ["importing", "sampling"].includes(active.state));
  const refreshActive = useCallback(async () => {
    if (!activeId) return;
    setActive(await api.importSession(activeId));
  }, [activeId]);

  useLiveQuery(moving ? ["files", "jobs"] : [], refreshActive, { fallbackMs: 5000 });

  /**
   * Import everything, in slices.
   *
   * A loop rather than one enormous request: 526 files is minutes of OCR
   * queueing, and a single call would hold a connection open long enough to be
   * killed by a proxy and leave you guessing how far it got. Each slice is
   * committed and idempotent, so stopping is always safe and resuming is just
   * calling it again.
   */
  async function importEverything() {
    setBusy(true);
    setNotice(null);
    stopRef.current = false;
    setRunningAll(true);
    try {
      let current = active;
      while (current && !stopRef.current) {
        const before = remainingOf(current);
        if (before === 0) break;
        current = await api.runImport(current.id, 50);
        setActive(current);
        if (remainingOf(current) >= before) {
          // No progress: something is refusing rather than finishing, and
          // looping on it would spin forever.
          setNotice("Stopped — that slice imported nothing. Check the failures.");
          break;
        }
      }
      if (current && remainingOf(current) === 0) setNotice("Everything imported.");
      else if (stopRef.current) setNotice("Stopped. Nothing was lost — resume any time.");
      await load();
    } catch (error) {
      setNotice(error instanceof ApiError ? error.message : "That didn't work.");
    } finally {
      setRunningAll(false);
      setBusy(false);
    }
  }

  async function act(fn: () => Promise<ImportSession>, message?: string) {
    setBusy(true);
    setNotice(null);
    try {
      setActive(await fn());
      if (message) setNotice(message);
      await load();
    } catch (error) {
      setNotice(error instanceof ApiError ? error.message : "That didn't work.");
    } finally {
      setBusy(false);
    }
  }

  const dry = active?.dry_run ?? {};
  const cost = active?.cost_estimate ?? {};
  const done = (active?.progress.ingested ?? 0) + (active?.progress.duplicate ?? 0);
  const total = Object.values(active?.progress ?? {}).reduce((a, b) => a + b, 0);

  return (
    <div className="mx-auto max-w-5xl">
      <PageHeader icon={ImportIcon} title="Import a backlog">
        Point this at a directory the <span className="font-mono text-xs">worker</span>{" "}
        container can see. Nothing is read into the archive until you&apos;ve reviewed
        what it found.
      </PageHeader>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          const library = libraries[0];
          if (!library || !path.trim()) return;
          void act(
            () => api.startImport({ library_id: library.id, root_path: path.trim() }),
            "Scanned. Nothing has been imported yet.",
          );
        }}
        className="flex flex-wrap gap-2 rounded-lg border border-edge bg-surface p-4"
      >
        <input
          value={path}
          onChange={(event) => setPath(event.target.value)}
          placeholder="/media/Main-Storage/Documents"
          className="min-w-0 flex-1 rounded-md border border-edge bg-ink px-3 py-2 font-mono text-sm outline-none focus:border-accent"
        />
        <button
          type="submit"
          disabled={busy || !path.trim()}
          className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-ink disabled:opacity-40"
        >
          {busy ? "Scanning…" : "Scan"}
        </button>
      </form>

      {notice && <p className="mt-3 text-sm text-accent">{notice}</p>}

      {sessions.length > 1 && (
        <div className="mt-4 flex flex-wrap gap-1">
          {sessions.map((s) => (
            <button
              key={s.id}
              onClick={() => setActive(s)}
              className={`rounded-md px-2 py-1 font-mono text-xs ${
                active?.id === s.id ? "bg-surface text-neutral-100" : "text-muted"
              }`}
            >
              {s.root_path.split("/").slice(-1)[0] || s.root_path} · {s.state}
            </button>
          ))}
        </div>
      )}

      {active && (
        <div className="mt-6 space-y-4">
          <section className="rounded-lg border border-edge bg-surface p-5">
            <div className="flex flex-wrap items-baseline justify-between gap-3">
              <h2 className="font-mono text-sm">{active.root_path}</h2>
              <span className="rounded-full border border-edge px-2 py-0.5 text-xs text-muted">
                {active.state.replace(/_/g, " ")}
                {active.pass_number > 1 && ` · pass ${active.pass_number}`}
              </span>
            </div>

            {active.last_error && (
              <p className="mt-3 rounded-md border border-red-500/40 p-3 text-sm text-red-300">
                {active.last_error}
              </p>
            )}

            <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-4">
              <Stat label="Files found" value={dry.total_files ?? 0} />
              <Stat label="Already here" value={dry.already_in_archive ?? 0} />
              <Stat label="Duplicates" value={dry.duplicates_within_batch ?? 0} />
              <Stat label="Pages (est.)" value={dry.estimated_pages ?? 0} />
            </dl>

            {dry.by_extension && Object.keys(dry.by_extension).length > 0 && (
              <p className="mt-3 font-mono text-xs text-muted">
                {Object.entries(dry.by_extension)
                  .map(([extension, count]) => `${extension} ${count}`)
                  .join("  ·  ")}
                {dry.skipped_unsupported ? `  ·  ${dry.skipped_unsupported} unsupported` : ""}
                {dry.skipped_hidden ? `  ·  ${dry.skipped_hidden} hidden` : ""}
              </p>
            )}

            {cost.batch_usd !== undefined && (
              <div
                className={`mt-4 rounded-md border p-3 text-sm ${
                  cost.exceeds_alarm ? "border-red-500/40" : "border-edge"
                }`}
              >
                {/*
                  Quote what this import will actually cost, which is the
                  interactive price — that is the only mode wired up. It
                  previously led with the batched figure, advertising a
                  half-price option with no way to choose it.
                */}
                <p>
                  Classifying this would cost about{" "}
                  <strong>${cost.interactive_usd?.toFixed(2)}</strong>.
                </p>
                <p className="mt-1 text-xs text-muted">
                  The Batch API would roughly halve that (about $
                  {cost.batch_usd?.toFixed(2)}) in exchange for results arriving
                  within a day instead of within minutes. It is not wired up yet,
                  so this import will run at the price above.
                </p>
                {cost.exceeds_alarm ? (
                  <p className="mt-1 text-red-300">
                    That's over the ${cost.alarm_threshold_usd} threshold the plan
                    set for this. The documented fallback is heuristics-only
                    segmentation plus manual correction — worth considering before
                    you run it.
                  </p>
                ) : (
                  <p className="mt-1 text-xs text-muted">
                    Estimated generously. OCR and search cost nothing — this is only
                    the classification step, and it's optional.
                  </p>
                )}
              </div>
            )}

            {total > 0 && (
              <div className="mt-4">
                <div className="mb-1 flex justify-between text-xs text-muted">
                  <span>
                    {done} of {total} handled
                  </span>
                  <span>
                    {Object.entries(active.progress)
                      .map(([state, count]) => `${count} ${state}`)
                      .join(" · ")}
                  </span>
                </div>
                <div className="h-1.5 overflow-hidden rounded-full bg-edge">
                  <div
                    className="h-full rounded-full bg-accent transition-all"
                    style={{ width: `${total ? (done / total) * 100 : 0}%` }}
                  />
                </div>
              </div>
            )}

            <div className="mt-4 flex flex-wrap gap-2">
              <button
                onClick={() => act(() => api.sampleImport(active.id), "Sample selected.")}
                disabled={busy}
                className="rounded-md border border-edge px-3 py-1.5 text-sm disabled:opacity-40"
              >
                Select a sample ({active.sample_size})
              </button>
              {runningAll ? (
                <button
                  onClick={() => {
                    stopRef.current = true;
                  }}
                  className="rounded-md border border-accent/60 px-3 py-1.5 text-sm text-accent"
                >
                  Stop after this slice
                </button>
              ) : (
                <button
                  onClick={() => void importEverything()}
                  disabled={busy || remainingOf(active) === 0}
                  className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-ink disabled:opacity-40"
                >
                  Import all {remainingOf(active).toLocaleString()}
                </button>
              )}
              <button
                onClick={() => act(() => api.runImport(active.id), "Imported a slice.")}
                disabled={busy}
                className="rounded-md border border-edge px-3 py-1.5 text-sm disabled:opacity-40"
              >
                Just the next 50
              </button>
              <button
                onClick={() => act(() => api.pauseImport(active.id), "Paused.")}
                disabled={busy}
                className="rounded-md border border-edge px-3 py-1.5 text-sm disabled:opacity-40"
              >
                Pause
              </button>
              <button
                onClick={() =>
                  act(
                    () => api.curateImport(active.id),
                    "Paused for curation. Tidy the taxonomy, then run pass two.",
                  )
                }
                disabled={busy}
                className="rounded-md border border-edge px-3 py-1.5 text-sm disabled:opacity-40"
              >
                Stop and curate
              </button>
              <button
                onClick={async () => setFailures(await api.importItems(active.id, "failed"))}
                className="rounded-md border border-edge px-3 py-1.5 text-sm text-muted"
              >
                Show failures
              </button>
            </div>

            <p className="mt-3 text-xs text-muted">
              Everything imported here is flagged as backlog and stays out of your
              daily{" "}
              <Link to="/review" className="underline underline-offset-2">
                review queue
              </Link>
              . It's searchable immediately either way.
            </p>
          </section>

          {failures.length > 0 && (
            <section className="rounded-lg border border-edge bg-surface p-4">
              <h3 className="mb-2 text-xs tracking-wide text-muted uppercase">
                Files that failed — {failures.length}
              </h3>
              <ul className="max-h-64 space-y-1 overflow-y-auto text-xs">
                {failures.map((item) => (
                  <li key={item.path} className="font-mono">
                    <span className="text-neutral-300">{item.path}</span>
                    <span className="ml-2 text-red-400/90">{item.error}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="font-mono text-lg">{value.toLocaleString()}</dd>
    </div>
  );
}
