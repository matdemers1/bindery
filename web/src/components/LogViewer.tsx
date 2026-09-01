import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  Info,
  RefreshCw,
  ScrollText,
  TriangleAlert,
  X,
} from "lucide-react";

import { ApiError, api, type LogEntry } from "../api";
import { useLiveQuery } from "../live/LiveProvider";

/**
 * The log, on screen.
 *
 * Everything the application logged used to go to the container's stdout, so
 * the answer to "why did this fail" was *ssh to the host and hope it has not
 * scrolled away*. This is the same output, kept, filterable, and attributable
 * to the file it was about.
 *
 * Levels are colour-coded because the reason anyone opens a log is to find the
 * red line, and defaulting the filter to warnings-and-above reflects that. The
 * traceback is collapsed rather than absent — it is the thing you need on the
 * one occasion you need it, and noise on every other.
 */
const LEVEL_STYLE: Record<string, string> = {
  critical: "text-red-300",
  error: "text-red-300",
  warning: "text-amber-300",
  info: "text-muted",
  debug: "text-muted",
};

export default function LogViewer({
  sourceFileId,
  title = "Log",
  onClose,
  defaultLevel = "",
}: {
  sourceFileId?: string;
  title?: string;
  onClose?: () => void;
  defaultLevel?: string;
}) {
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [cursor, setCursor] = useState<number | null>(null);
  const [pending, setPending] = useState(0);
  const [level, setLevel] = useState(defaultLevel);
  const [q, setQ] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [live, setLive] = useState(true);

  const load = useCallback(
    async (append = false) => {
      setLoading(true);
      try {
        const page = await api.logs({
          source_file_id: sourceFileId,
          level: level || undefined,
          q: q || undefined,
          before_sequence: append && cursor ? cursor : undefined,
          limit: 100,
        });
        setEntries((current) => (append ? [...current, ...page.entries] : page.entries));
        setCursor(page.next_before_sequence);
        setPending(page.pending_writes);
        setError(null);
      } catch (caught) {
        setError(caught instanceof ApiError ? caught.message : String(caught));
      } finally {
        setLoading(false);
      }
      // `cursor` excluded on purpose: including it would reload the first page
      // every time paging advanced it.
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [sourceFileId, level, q],
  );

  // `live` is now about whether to accept pushes rather than how fast to ask.
  useLiveQuery(live ? ["logs", "jobs"] : [], load, { fallbackMs: 10_000 });

  useEffect(() => {
    if (live) return;
    // Paused: load once so the pane is not blank, then leave it alone.
    // An async data load: the state is genuinely unavailable on the first
    // render, so the extra pass is the point rather than a mistake.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [live, load]);

  return (
    <section className="flex min-h-0 flex-col rounded-xl border border-edge bg-surface">
      <header className="flex flex-wrap items-center gap-2 border-b border-edge px-4 py-3">
        <h2 className="flex items-center gap-2 text-sm font-medium">
          <ScrollText size={15} className="text-accent" />
          {title}
        </h2>

        <span className="flex-1" />

        <label className="sr-only" htmlFor="log-level">
          Minimum level
        </label>
        <select
          id="log-level"
          value={level}
          onChange={(event) => setLevel(event.target.value)}
          className="rounded border border-edge bg-ink px-2 py-1 text-xs"
        >
          <option value="">Everything</option>
          <option value="warning">Warnings and errors</option>
          <option value="error">Errors only</option>
        </select>

        <label className="sr-only" htmlFor="log-search">
          Filter messages
        </label>
        <input
          id="log-search"
          value={q}
          onChange={(event) => setQ(event.target.value)}
          placeholder="Filter…"
          className="w-40 rounded border border-edge bg-ink px-2 py-1 text-xs"
        />

        <button
          type="button"
          onClick={() => setLive((value) => !value)}
          title={live ? "Following new entries" : "Paused"}
          className={`rounded border px-2 py-1 text-xs ${
            live ? "border-accent/60 text-accent" : "border-edge text-muted"
          }`}
        >
          <RefreshCw size={12} className={live ? "animate-spin" : ""} />
        </button>

        {onClose && (
          <button
            type="button"
            onClick={onClose}
            aria-label="Close log"
            className="rounded p-1 text-muted hover:text-neutral-100"
          >
            <X size={15} />
          </button>
        )}
      </header>

      {pending > 0 && (
        <p className="border-b border-edge bg-amber-950/20 px-4 py-1.5 text-xs text-amber-300">
          {pending} entries are still being written — what you are reading is slightly
          behind.
        </p>
      )}

      {error && (
        <p role="alert" className="px-4 py-3 text-sm text-red-300">
          {error}
        </p>
      )}

      <ol className="max-h-[28rem] min-h-0 flex-1 divide-y divide-edge/60 overflow-y-auto">
        {entries.length === 0 && !loading && (
          <li className="px-4 py-6 text-center text-sm text-muted">
            Nothing logged{sourceFileId ? " for this file" : ""} yet.
          </li>
        )}
        {entries.map((entry) => (
          <li key={entry.id} className="px-4 py-2">
            <div className="flex items-start gap-2">
              {entry.level === "error" || entry.level === "critical" ? (
                <AlertTriangle size={13} className="mt-0.5 shrink-0 text-red-400" />
              ) : entry.level === "warning" ? (
                <TriangleAlert size={13} className="mt-0.5 shrink-0 text-amber-400" />
              ) : (
                <Info size={13} className="mt-0.5 shrink-0 text-muted" />
              )}
              <span className="shrink-0 font-mono text-[11px] text-muted">
                {new Date(entry.created_at).toLocaleTimeString()}
              </span>
              {entry.stage && (
                <span className="shrink-0 rounded bg-edge px-1.5 text-[11px] text-neutral-300">
                  {entry.stage}
                </span>
              )}
              <span className={`min-w-0 text-sm ${LEVEL_STYLE[entry.level] ?? ""}`}>
                {entry.message}
              </span>
            </div>

            {entry.detail && (
              <details className="ml-[1.4rem] mt-1">
                <summary className="cursor-pointer text-[11px] text-muted">
                  traceback
                </summary>
                <pre className="mt-1 max-h-60 overflow-auto rounded bg-ink p-2 font-mono text-[11px] leading-relaxed text-neutral-300">
                  {entry.detail}
                </pre>
              </details>
            )}
          </li>
        ))}
      </ol>

      {cursor !== null && (
        <div className="border-t border-edge p-2">
          <button
            type="button"
            onClick={() => void load(true)}
            disabled={loading}
            className="w-full rounded border border-edge px-3 py-1.5 text-xs disabled:opacity-40"
          >
            {loading ? "Loading…" : "Load older"}
          </button>
        </div>
      )}
    </section>
  );
}
