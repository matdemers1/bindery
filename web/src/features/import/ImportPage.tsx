import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  FolderInput,
  Import as ImportIcon,
  Inbox,
  Lock,
  LockOpen,
  ShieldCheck,
} from "lucide-react";

import {
  ApiError,
  api,
  type ImportItem,
  type ImportLogLine,
  type ImportSession,
  type Library,
  type VaultState,
} from "../../api";
import PageHeader from "../../components/PageHeader";
import { useLiveQuery } from "../../live/LiveProvider";
import UnlockForm from "../vault/UnlockForm";

/**
 * Import (T-4.1 to T-4.5; redesigned in Phase 18, REQ-196, REQ-197).
 *
 * Three steps, in order, on one screen: **where** (the inbox in one click, or
 * a path), **what it found** (the scan, and what it would cost), **go**. Then
 * a history that answers the question the old screen could not: what happened
 * — imported, duplicated, skipped, failed, with the reasons and the worker's
 * own log lines.
 *
 * The risk this screen exists to manage is R-03 — that importing five thousand
 * documents floods the daily review queue and the project gets abandoned.
 * Imported documents are flagged and kept out of that queue, and the screen
 * says so.
 *
 * **Into the vault** seals every file as its pipeline finishes, while the
 * vault is open. The worker cannot seal (the key never leaves the api), so the
 * api does it on a short sweep — and if the vault locks mid-import the rest
 * wait, visibly, as "unlock to continue" rather than looking done.
 */
function remainingOf(session: ImportSession): number {
  const progress = session.progress ?? {};
  return (progress.pending ?? 0) + (progress.sampled ?? 0);
}

const STATE_LABEL: Record<string, string> = {
  ingested: "imported",
  duplicate: "already here",
  skipped: "skipped",
  failed: "failed",
  pending: "waiting",
  sampled: "in the sample",
};

export default function ImportPage({ libraries }: { libraries: Library[] }) {
  const [sessions, setSessions] = useState<ImportSession[]>([]);
  const [active, setActive] = useState<ImportSession | null>(null);
  const [inbox, setInbox] = useState<string | null>(null);
  const [vault, setVault] = useState<VaultState | null>(null);
  const [path, setPath] = useState("");
  const [toVault, setToVault] = useState(false);
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [runningAll, setRunningAll] = useState(false);
  const stopRef = useRef(false);

  const load = useCallback(async () => {
    const [all, presets, vaultState] = await Promise.all([
      api.imports(),
      api.importPresets(),
      api.vault().catch(() => null),
    ]);
    setSessions(all);
    setInbox(presets.inbox);
    setVault(vaultState);
    setActive((current) =>
      current ? all.find((s) => s.id === current.id) ?? null : all[0] ?? null,
    );
  }, []);

  // No topics: the live half of this screen is `refreshActive` below, which
  // subscribes only while a move is running. The session list itself changes
  // when you change it.
  useLiveQuery([], load);

  const activeId = active?.id;
  const moving = Boolean(
    active && (["importing", "sampling"].includes(active.state) || (active.to_vault && active.awaiting_vault > 0)),
  );
  const refreshActive = useCallback(async () => {
    if (!activeId) return;
    setActive(await api.importSession(activeId));
  }, [activeId]);
  useLiveQuery(moving ? ["files", "jobs", "documents"] : [], refreshActive, { fallbackMs: 5000 });

  async function scan(root: string) {
    const library = libraries[0];
    if (!library || !root.trim()) return;
    setBusy(true);
    setNotice(null);
    try {
      const made = await api.startImport({
        library_id: library.id,
        root_path: root.trim(),
        to_vault: toVault,
      });
      setActive(made);
      setTouched(false);
      setNotice("Scanned. Nothing has been imported yet — review what it found below.");
      await load();
    } catch (error) {
      setNotice(error instanceof ApiError ? error.message : "That didn't work.");
    } finally {
      setBusy(false);
    }
  }

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
          setNotice("Stopped — that slice imported nothing. See what failed below.");
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

  const vaultReady = Boolean(vault?.exists && vault.unlocked);

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <PageHeader icon={ImportIcon} title="Import">
        Bring a folder of files into the archive. Nothing is read in until you have
        seen what was found and what it would cost.
      </PageHeader>

      {/* ---- 1. Where ------------------------------------------------------ */}
      <section className="space-y-3 rounded-xl border border-edge bg-surface p-4">
        <h2 className="text-xs tracking-wide text-muted uppercase">1 · Where from</h2>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => inbox && void scan(inbox)}
            disabled={busy || !inbox}
            title={inbox ?? ""}
            className="flex items-center gap-2 rounded-lg bg-accent px-3 py-2 text-sm font-medium text-ink disabled:opacity-40"
          >
            <Inbox size={15} />
            {busy ? "Scanning…" : "Scan the inbox"}
          </button>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void scan(path);
            }}
            className="flex min-w-0 flex-1 gap-2"
          >
            <input
              aria-label="A folder the worker can see"
              value={path}
              onChange={(event) => setPath(event.target.value)}
              placeholder={`…or a folder the worker can see, e.g. ${inbox ?? "/data/inbox"}/2019`}
              className="min-w-0 flex-1 rounded-lg border border-field bg-ink px-3 py-2 font-mono text-sm outline-none focus:border-accent"
            />
            <button
              type="submit"
              disabled={busy || !path.trim()}
              className="flex items-center gap-1.5 rounded-lg border border-field px-3 py-2 text-sm disabled:opacity-40"
            >
              <FolderInput size={14} /> Scan
            </button>
          </form>
        </div>

        {/* Into the vault. Only offered as a real choice when it can be honoured. */}
        <label
          className={`flex items-start gap-2 text-sm ${vaultReady ? "" : "text-muted"}`}
        >
          <input
            type="checkbox"
            // Optimistic: reflects the click at once rather than after the
            // round-trip, or the box appears not to respond for a beat.
            checked={(touched ? toVault : active ? active.to_vault : toVault) && vaultReady}
            disabled={!vaultReady}
            onChange={(event) => {
              const wanted = event.target.checked;
              setTouched(true);
              setToVault(wanted);
              // Persisted the moment it is ticked, on the import that is on
              // screen. The first version kept this in the browser until the
              // scan, so ticking it *after* scanning — the natural order —
              // was dropped in silence, and 309 files went into the ordinary
              // archive with the box ticked.
              if (active) {
                void act(() => api.setImportVault(active.id, wanted),
                  wanted
                    ? "This import now goes into the vault. Files already imported are sealed as soon as your vault is open."
                    : "This import will stay in the ordinary archive.");
              }
            }}
            className="mt-0.5"
          />
          <span>
            <span className="flex items-center gap-1.5">
              <ShieldCheck size={14} /> Straight into the vault
            </span>
            <span className="block text-xs text-muted">
              {vault?.exists === false
                ? "You have no vault yet — set one up under Vault first."
                : vaultReady
                  ? "Every file is encrypted and taken out of the archive the moment its processing finishes, while your vault is open."
                  : "Your vault is locked. Unlock it to import into it — files are sealed as they finish, and that needs the vault open."}
            </span>
          </span>
        </label>
        {vault?.exists && !vault.unlocked && (
          <div className="max-w-sm">
            <UnlockForm state={vault} onUnlocked={setVault} compact />
          </div>
        )}
      </section>

      {/* Always in the DOM, `sr-only` while empty — a live region inserted at
          the same moment as its text is not reliably announced, and one of the
          things this says is that 309 files just changed destination. */}
      <p
        role="status"
        aria-live="polite"
        className={notice ? "text-sm text-accent" : "sr-only"}
      >
        {notice}
      </p>

      {/* ---- 2 & 3. What it found, and go ---------------------------------- */}
      {active && <ActiveRun run={active} busy={busy} runningAll={runningAll} stopRef={stopRef} act={act} importEverything={importEverything} />}

      {/* ---- History -------------------------------------------------------- */}
      {sessions.length > 0 && (
        <section className="space-y-2">
          <h2 className="text-xs tracking-wide text-muted uppercase">Previous imports</h2>
          <ul className="space-y-2">
            {sessions.map((run) => (
              <RunRow key={run.id} run={run} isActive={active?.id === run.id} onOpen={() => setActive(run)} />
            ))}
          </ul>
        </section>
      )}

      <p className="text-xs text-muted">
        Everything imported here is flagged as backlog and stays out of your daily{" "}
        <Link to="/review" className="underline underline-offset-2">
          review queue
        </Link>
        . It is searchable immediately either way.
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------

function ActiveRun({
  run,
  busy,
  runningAll,
  stopRef,
  act,
  importEverything,
}: {
  run: ImportSession;
  busy: boolean;
  runningAll: boolean;
  stopRef: React.MutableRefObject<boolean>;
  act: (fn: () => Promise<ImportSession>, message?: string) => Promise<void>;
  importEverything: () => Promise<void>;
}) {
  const dry = run.dry_run ?? {};
  const cost = run.cost_estimate ?? {};
  const done = (run.progress.ingested ?? 0) + (run.progress.duplicate ?? 0) + (run.progress.skipped ?? 0) + (run.progress.failed ?? 0);
  const total = Object.values(run.progress ?? {}).reduce((a, b) => a + b, 0);
  const remaining = remainingOf(run);

  return (
    <section className="space-y-4 rounded-xl border border-edge bg-surface p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h2 className="text-xs tracking-wide text-muted uppercase">2 · What it found</h2>
        <span className="font-mono text-xs text-muted">{run.root_path}</span>
      </div>

      {run.last_error && (
        <p className="rounded-md border border-red-500/40 p-3 text-sm text-red-300">{run.last_error}</p>
      )}

      <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-4">
        <Stat label="Files found" value={dry.total_files ?? 0} />
        <Stat label="Already here" value={dry.already_in_archive ?? 0} />
        <Stat label="Duplicates" value={dry.duplicates_within_batch ?? 0} />
        <Stat label="Pages (est.)" value={dry.estimated_pages ?? 0} />
      </dl>

      {dry.by_extension && Object.keys(dry.by_extension).length > 0 && (
        <p className="font-mono text-xs text-muted">
          {Object.entries(dry.by_extension).map(([ext, n]) => `${ext} ${n}`).join("  ·  ")}
          {dry.skipped_unsupported ? `  ·  ${dry.skipped_unsupported} unsupported` : ""}
          {dry.skipped_hidden ? `  ·  ${dry.skipped_hidden} hidden` : ""}
        </p>
      )}

      {cost.interactive_usd !== undefined && (
        <div className={`rounded-md border p-3 text-sm ${cost.exceeds_alarm ? "border-red-500/40" : "border-edge"}`}>
          <p>
            AI review of these would cost about <strong>${cost.interactive_usd.toFixed(2)}</strong>.
          </p>
          <p className="mt-1 text-xs text-muted">
            OCR, search and videos cost nothing. This is only the classification step, and it is optional —
            imported files are searchable before any of it runs.
          </p>
          {cost.exceeds_alarm && (
            <p className="mt-1 text-red-300">
              Over the ${cost.alarm_threshold_usd} threshold the plan set. Worth a look before you run it.
            </p>
          )}
        </div>
      )}

      {run.to_vault && <VaultProgress run={run} />}

      {total > 0 && (
        <div>
          <div className="mb-1 flex justify-between text-xs text-muted">
            <span>{done} of {total} handled</span>
            <span>{remaining} to go</span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-edge">
            <div className="h-full rounded-full bg-accent transition-all" style={{ width: `${total ? (done / total) * 100 : 0}%` }} />
          </div>
        </div>
      )}

      <div>
        <h2 className="mb-2 text-xs tracking-wide text-muted uppercase">3 · Go</h2>
        <div className="flex flex-wrap gap-2">
          {runningAll ? (
            <button
              onClick={() => { stopRef.current = true; }}
              className="rounded-md border border-accent/60 px-3 py-1.5 text-sm text-accent"
            >
              Stop after this slice
            </button>
          ) : (
            <button
              onClick={() => void importEverything()}
              disabled={busy || remaining === 0}
              className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-ink disabled:opacity-40"
            >
              Import all {remaining.toLocaleString()}
            </button>
          )}
          <button onClick={() => act(() => api.runImport(run.id), "Imported a slice.")} disabled={busy || remaining === 0} className="rounded-md border border-field px-3 py-1.5 text-sm disabled:opacity-40">
            Just the next 50
          </button>
          <button onClick={() => act(() => api.sampleImport(run.id), "Sample selected.")} disabled={busy} className="rounded-md border border-field px-3 py-1.5 text-sm disabled:opacity-40">
            Try a sample of {run.sample_size}
          </button>
          <button onClick={() => act(() => api.pauseImport(run.id), "Paused.")} disabled={busy} className="rounded-md border border-field px-3 py-1.5 text-sm disabled:opacity-40">
            Pause
          </button>
          <button onClick={() => act(() => api.curateImport(run.id), "Paused for curation. Tidy the taxonomy, then run pass two.")} disabled={busy} className="rounded-md border border-field px-3 py-1.5 text-sm text-muted disabled:opacity-40">
            Stop and curate
          </button>
        </div>
      </div>

      <RunDetail run={run} defaultOpen />
    </section>
  );
}

function VaultProgress({ run }: { run: ImportSession }) {
  const imported = run.progress.ingested ?? 0;
  const inFlight = Math.max(0, imported - run.vaulted - run.awaiting_vault);
  return (
    <div className="rounded-md border border-accent/40 bg-accent/5 p-3 text-sm">
      <p className="flex items-center gap-1.5 text-accent">
        <ShieldCheck size={14} /> Into the vault
      </p>
      <p className="mt-1 text-xs text-muted">
        {run.vaulted} sealed
        {inFlight > 0 && ` · ${inFlight} still being processed`}
        {run.awaiting_vault > 0 && ` · ${run.awaiting_vault} finished and waiting`}
      </p>
      {run.awaiting_vault > 0 && !run.vault_unlocked && (
        <p className="mt-2 flex items-center gap-1.5 text-xs text-amber-300">
          <Lock size={12} /> Your vault is locked. {run.awaiting_vault} file{run.awaiting_vault === 1 ? " is" : "s are"} finished and waiting —
          <Link to="/vault" className="underline underline-offset-2">unlock it</Link> and they seal within a few seconds.
        </p>
      )}
      {run.awaiting_vault > 0 && run.vault_unlocked && (
        <p className="mt-2 flex items-center gap-1.5 text-xs text-muted">
          <LockOpen size={12} /> Sealing now.
        </p>
      )}
    </div>
  );
}

function RunRow({ run, isActive, onOpen }: { run: ImportSession; isActive: boolean; onOpen: () => void }) {
  const [open, setOpen] = useState(false);
  const p = run.progress ?? {};
  const failed = p.failed ?? 0;
  const label = run.root_path.split("/").filter(Boolean).slice(-1)[0] || run.root_path;
  return (
    <li className={`rounded-lg border bg-surface ${isActive ? "border-accent/60" : "border-edge"}`}>
      <div className="flex flex-wrap items-center gap-3 px-3 py-2">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          aria-label={open ? "Hide details" : "Show details"}
        >
          {open ? <ChevronDown size={15} aria-hidden /> : <ChevronRight size={15} aria-hidden />}
        </button>
        <button type="button" onClick={onOpen} className="min-w-0 flex-1 text-left">
          <span className="block truncate font-mono text-sm">{label}</span>
          <span className="block text-xs text-muted">
            {run.created_at.slice(0, 16).replace("T", " ")} · {run.state.replace(/_/g, " ")}
            {run.to_vault && " · into the vault"}
          </span>
        </button>
        <span className="flex items-center gap-3 text-xs">
          {(p.ingested ?? 0) > 0 && <span className="flex items-center gap-1 text-emerald-400"><CheckCircle2 size={12} /> {p.ingested} imported</span>}
          {(p.duplicate ?? 0) > 0 && <span className="text-muted">{p.duplicate} already here</span>}
          {failed > 0 && <span className="flex items-center gap-1 text-red-300"><AlertTriangle size={12} /> {failed} failed</span>}
          {remainingOf(run) > 0 && <span className="text-muted">{remainingOf(run)} waiting</span>}
        </span>
      </div>
      {open && <div className="border-t border-edge p-3"><RunDetail run={run} /></div>}
    </li>
  );
}

/** What happened, per file and in the worker's own words (REQ-196). */
function RunDetail({ run, defaultOpen = false }: { run: ImportSession; defaultOpen?: boolean }) {
  const [tab, setTab] = useState<"failed" | "ingested" | "duplicate" | "skipped" | "log">("failed");
  const counts = run.progress ?? {};
  void defaultOpen;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-1 text-xs">
        {(["failed", "ingested", "duplicate", "skipped"] as const).map((state) => (
          <button
            key={state}
            type="button"
            // Which filter is applied was a background fill and nothing else.
            // `aria-pressed` rather than the tab pattern because these narrow
            // a list in place; they do not swap a panel.
            aria-pressed={tab === state}
            onClick={() => setTab(state)}
            className={`rounded-md px-2 py-1 ${tab === state ? "bg-ink text-neutral-100" : "text-muted hover:text-neutral-100"}`}
          >
            {STATE_LABEL[state]} <span className="text-muted">{counts[state] ?? 0}</span>
          </button>
        ))}
        <button
          type="button"
          aria-pressed={tab === "log"}
          onClick={() => setTab("log")}
          className={`rounded-md px-2 py-1 ${tab === "log" ? "bg-ink text-neutral-100" : "text-muted hover:text-neutral-100"}`}
        >
          worker log
        </button>
      </div>
      {/* Keyed on the tab so switching remounts the list: the previous tab's
          rows can never show under the new tab's heading, and no effect has
          to reset state by hand. */}
      <RunDetailBody key={`${run.id}:${tab}`} run={run} tab={tab} />
    </div>
  );
}

function RunDetailBody({
  run,
  tab,
}: {
  run: ImportSession;
  tab: "failed" | "ingested" | "duplicate" | "skipped" | "log";
}) {
  const [items, setItems] = useState<ImportItem[] | null>(null);
  const [log, setLog] = useState<ImportLogLine[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load =
      tab === "log"
        ? api.importLog(run.id).then((rows) => {
            if (!cancelled) setLog(rows);
          })
        : api.importItems(run.id, tab).then((rows) => {
            if (!cancelled) setItems(rows);
          });
    load.catch((caught) => {
      if (!cancelled) setError(caught instanceof Error ? caught.message : String(caught));
    });
    return () => {
      cancelled = true;
    };
  }, [run.id, tab]);

  if (error) return <p className="text-xs text-red-300">{error}</p>;
  if (tab === "log") {
    if (log === null) return <p className="text-xs text-muted">Loading…</p>;
    if (log.length === 0) {
      return <p className="text-xs text-muted">The worker has not written anything about these files yet.</p>;
    }
    return (
      <ul className="max-h-64 space-y-1 overflow-y-auto font-mono text-[11px]">
        {log.map((line, i) => (
          <li key={i} className={line.level === "ERROR" ? "text-red-300" : line.level === "WARNING" ? "text-amber-300" : "text-neutral-300"}>
            <span className="text-muted">{line.at.slice(11, 19)}</span>{" "}
            {line.stage && <span className="text-muted">[{line.stage}]</span>} {line.message}
          </li>
        ))}
      </ul>
    );
  }
  if (items === null) return <p className="text-xs text-muted">Loading…</p>;
  if (items.length === 0) return <p className="text-xs text-muted">Nothing {STATE_LABEL[tab]}.</p>;
  return (
    <ul className="max-h-64 space-y-1 overflow-y-auto font-mono text-[11px]">
      {items.map((item) => (
        <li key={item.path} className="flex flex-wrap gap-x-2">
          {item.source_file_id ? (
            <Link to={`/file/${item.source_file_id}`} className="text-neutral-300 underline-offset-2 hover:underline">{item.path}</Link>
          ) : (
            <span className="text-neutral-300">{item.path}</span>
          )}
          {item.error && <span className="text-red-400/90">{item.error}</span>}
        </li>
      ))}
    </ul>
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
