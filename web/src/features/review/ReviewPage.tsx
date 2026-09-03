import PageThumb from "../../components/PageThumb";
import { ClipboardCheck, Pencil } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router";

import { ApiError, api, type Document, type DocumentDetail } from "../../api";
import { isTypingTarget } from "../../lib/keyboard";
import { useLiveQuery } from "../../live/LiveProvider";
import EditPanel from "../edit/EditPanel";
import WhyPanel from "../why/WhyPanel";

/**
 * Keyboard triage (T-3.10): preview left, provenance right.
 *
 * The queue only contains what the gate declined to file unattended, so every
 * item here is a real question rather than a rubber stamp. `A` accepts, `U`
 * undoes, `J`/`K` move — the whole point is that a session of triage costs
 * keystrokes, not mouse travel, because a queue that is tiring to work is a
 * queue that gets abandoned (R-03).
 */
export default function ReviewPage() {
  const [documents, setDocuments] = useState<Document[]>([]);
  const [total, setTotal] = useState(0);
  const [index, setIndex] = useState(0);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [correcting, setCorrecting] = useState<DocumentDetail | null>(null);

  const load = useCallback(async () => {
    const queue = await api.review();
    setDocuments(queue.documents);
    setTotal(queue.total);
    setIndex((current) => Math.min(current, Math.max(queue.documents.length - 1, 0)));
    setLoaded(true);
  }, []);

  // Every route that changes what is waiting — the worker finishing a
  // classification, an accept, an undo, a document edited from anywhere else —
  // publishes `review`, so this is exactly the set that makes the queue stale.
  // A mount-only load is what made the badge and this screen disagree about
  // work that had already been accepted.
  useLiveQuery(["review"], load);

  const current = documents[index];

  const act = useCallback(
    async (action: "accept" | "undo") => {
      if (!current || busy) return;
      setBusy(true);
      setNotice(null);
      try {
        if (action === "accept") {
          await api.accept(current.id);
          setNotice("Filed.");
        } else {
          await api.undo(current.id);
          setNotice("Reverted the last automated decision.");
        }
        await load();
      } catch (error) {
        setNotice(
          error instanceof ApiError && error.status === 409
            ? "There is nothing left to undo on this document."
            : "That didn't work.",
        );
      } finally {
        setBusy(false);
      }
    },
    [current, busy, load],
  );

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (isTypingTarget(event.target)) return;
      if (event.metaKey || event.ctrlKey) return;
      const key = event.key.toLowerCase();
      if (key === "j") setIndex((n) => Math.min(n + 1, documents.length - 1));
      if (key === "k") setIndex((n) => Math.max(n - 1, 0));
      if (key === "a") void act("accept");
      if (key === "u") void act("undo");
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [documents.length, act]);

  const pages = useMemo(
    () =>
      current
        ? Array.from(
            { length: Math.min(current.page_end - current.page_start + 1, 4) },
            (_, offset) => current.page_start + offset,
          )
        : [],
    [current],
  );

  if (!loaded) {
    return <div className="mx-auto max-w-4xl py-8 text-center text-muted">Loading…</div>;
  }

  if (!current) {
    return (
      <div className="mx-auto max-w-2xl py-12 text-center">
        <p className="text-lg">Nothing waiting.</p>
        <p className="mt-2 text-sm text-muted">Everything filed itself.</p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-7xl">
      <header className="mb-4 flex flex-wrap items-baseline justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2.5 text-xl font-semibold tracking-tight">
            <ClipboardCheck size={19} className="text-accent" />
            Review
          </h1>
          <p className="text-sm text-muted">
            {index + 1} of {documents.length}
            {total > documents.length && ` (${total} waiting)`} · these are the documents
            the gate would not file on its own
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Key label="J">next</Key>
          <Key label="K">previous</Key>
          <Key label="A">accept</Key>
          <Key label="U">undo</Key>
        </div>
      </header>

      {/* Always in the DOM, `sr-only` while empty: a live region inserted at
          the same moment as its text is not reliably announced — and on this
          screen the confirmation is the only evidence that Accept did
          anything, because the card silently advances to the next one. */}
      <p
        role="status"
        aria-live="polite"
        className={notice ? "mb-3 text-sm text-accent" : "sr-only"}
      >
        {notice}
      </p>

      <div className="grid gap-6 lg:grid-cols-[1fr_24rem]">
        <div>
          <div className="mb-3 flex flex-wrap items-baseline justify-between gap-3">
            <h2 className="min-w-0 truncate text-base">
              {current.title ?? <span className="text-muted">(untitled)</span>}
            </h2>
            <div className="flex gap-2">
              <button
                onClick={() => act("accept")}
                disabled={busy}
                className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-ink disabled:opacity-40"
              >
                Accept
              </button>
              {/* The case the queue had no expression for. Until now the only
                  answers were "accept all of it" and "throw all of it away",
                  so "this is right except the date" meant accepting something
                  wrong or rejecting something mostly right. */}
              <button
                aria-pressed={Boolean(correcting)}
                onClick={() => {
                  if (correcting) {
                    setCorrecting(null);
                    return;
                  }
                  void api
                    .document(current.id)
                    .then(setCorrecting)
                    .catch(() => setNotice("Could not open this for editing."));
                }}
                disabled={busy}
                className={`flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm disabled:opacity-40 ${
                  correcting
                    ? "border-accent/60 bg-accent/10 text-accent"
                    : "border-edge hover:border-accent/60"
                }`}
              >
                <Pencil size={14} />
                Correct
              </button>
              <button
                onClick={() => act("undo")}
                disabled={busy}
                className="rounded-md border border-edge px-3 py-1.5 text-sm disabled:opacity-40"
              >
                Undo
              </button>
              <Link
                to={`/document/${current.id}/page/1`}
                className="rounded-md border border-edge px-3 py-1.5 text-sm text-muted hover:border-accent/60"
              >
                Open
              </Link>
            </div>
          </div>

          {correcting && correcting.document.id === current.id && (
            <div className="mb-3">
              <EditPanel
                detail={correcting}
                onCancel={() => setCorrecting(null)}
                onSaved={(message) => {
                  setCorrecting(null);
                  setNotice(`${message} — accept when you are happy with it.`);
                  void load();
                }}
              />
            </div>
          )}

          <ul className="flex gap-2 overflow-x-auto rounded-lg border border-edge bg-surface p-3">
            {pages.map((page) => (
              <li key={page}>
                <PageThumb
                  sourceFileId={current.source_file_id}
                  page={page}
                  className="h-64 w-48 rounded border border-edge object-contain"
                />
              </li>
            ))}
          </ul>
        </div>

        <WhyPanel documentId={current.id} fileId={current.source_file_id} />
      </div>
    </div>
  );
}

function Key({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <span className="flex items-center gap-1 text-xs text-muted">
      <kbd className="rounded border border-edge bg-surface px-1.5 py-0.5 font-mono">
        {label}
      </kbd>
      {children}
    </span>
  );
}
