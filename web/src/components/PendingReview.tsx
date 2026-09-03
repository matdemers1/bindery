import { useCallback, useState } from "react";

import { ApiError, api, type PendingReview } from "../api";
import { useLiveQuery } from "../live/LiveProvider";

/**
 * "Run AI review over the documents that missed it."
 *
 * The archive is deliberately useful with no API key — OCR, indexing and search
 * all work — so the ordinary way to use it is to add documents first and a key
 * later. That makes this a normal operation, not a recovery procedure, and
 * until now the only way to do it was a shell on the host.
 *
 * The three reasons are kept apart because they are different situations
 * wearing the same face. "These arrived before you had a key" is not a failure
 * and is not styled as one; "these gave up for another reason" is.
 *
 * Rendered on Pipeline and on Settings — Settings because the moment you finish
 * pasting a key is exactly when you want to be asked.
 */
const TONE: Record<string, string> = {
  never_attempted: "border-edge bg-surface",
  provider_unavailable: "border-amber-900/60 bg-amber-950/20",
  failed: "border-red-900/60 bg-red-950/20",
};

export default function PendingReviewPanel({ compact = false }: { compact?: boolean }) {
  const [pending, setPending] = useState<PendingReview | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setPending(await api.pendingReview());
    } catch {
      // Informational panel; a failed poll should not take over the screen.
    }
  }, []);

  // This panel is half of the pair CLAUDE.md names as the reason live updates
  // exist: it counts documents nothing has reviewed, and everything that
  // changes that count — the worker finishing a classification, a reclassify
  // this panel itself queued, a job giving up — publishes `review` or `jobs`.
  // Loading once on mount left it stating a number that had stopped being true.
  useLiveQuery(["review", "jobs"], load);

  async function run(reason: string | null) {
    setBusy(reason ?? "all");
    setError(null);
    setNotice(null);
    try {
      const result = await api.reclassify({
        all_pending: true,
        reasons: reason ? [reason] : [],
      });
      setNotice(
        result.queued === 0
          ? "Nothing needed queueing."
          : `Queued ${result.queued} document${result.queued === 1 ? "" : "s"}. ` +
            "They will be titled, dated and tagged as the worker gets to them.",
      );
      await load();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(null);
    }
  }

  // Silence is the correct output when there is nothing waiting: a panel
  // explaining a problem you do not have is noise on every other visit.
  if (!pending || pending.total === 0) return null;

  return (
    <section className="rounded-lg border border-edge bg-surface p-5">
      <h2 className="text-base font-medium">
        {pending.total} document{pending.total === 1 ? "" : "s"} without AI review
      </h2>
      {!compact && (
        <p className="mt-1 max-w-2xl text-sm text-muted">
          These are searchable already — every page is OCR'd and indexed whether or not
          a model ever sees it. What they are missing is a title, a date and tags.
        </p>
      )}

      <ul className="mt-4 space-y-3">
        {pending.reasons.map((reason) => (
          <li key={reason.code} className={`rounded border p-3 ${TONE[reason.code] ?? ""}`}>
            <div className="flex flex-wrap items-baseline gap-2">
              <span className="text-sm font-medium">{reason.label}</span>
              <span className="text-xs text-muted">
                {reason.count} document{reason.count === 1 ? "" : "s"}
              </span>
            </div>
            <p className="mt-1 max-w-2xl text-sm text-muted">{reason.detail}</p>
            <button
              type="button"
              onClick={() => void run(reason.code)}
              disabled={busy !== null}
              className="mt-2 rounded border border-edge px-2.5 py-1 text-xs disabled:opacity-40"
            >
              {busy === reason.code ? "Queueing…" : `Run AI review on these ${reason.count}`}
            </button>
          </li>
        ))}
      </ul>

      {pending.reasons.length > 1 && (
        <button
          type="button"
          onClick={() => void run(null)}
          disabled={busy !== null}
          className="mt-3 rounded bg-accent px-3 py-1.5 text-sm font-medium text-ink disabled:opacity-40"
        >
          {busy === "all" ? "Queueing…" : `Run AI review on all ${pending.total}`}
        </button>
      )}

      {notice && <p className="mt-3 text-sm text-muted">{notice}</p>}
      {error && (
        <p role="alert" className="mt-3 text-sm text-red-300">
          {error}
        </p>
      )}
    </section>
  );
}
