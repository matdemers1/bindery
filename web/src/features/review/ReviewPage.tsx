import PageThumb from "../../components/PageThumb";
import { ClipboardCheck } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router";

import { ApiError, api, type Document } from "../../api";
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

  const load = useCallback(async () => {
    const queue = await api.review();
    setDocuments(queue.documents);
    setTotal(queue.total);
    setIndex((current) => Math.min(current, Math.max(queue.documents.length - 1, 0)));
    setLoaded(true);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

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
      if (event.target instanceof HTMLInputElement) return;
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

      {notice && <p className="mb-3 text-sm text-accent">{notice}</p>}

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
