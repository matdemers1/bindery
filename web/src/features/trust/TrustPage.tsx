import { ShieldCheck } from "lucide-react";

import PageHeader from "../../components/PageHeader";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router";

import { useLiveQuery } from "../../live/LiveProvider";
import {
  AlertTriangle,
  Clock,
  Loader,
  type LucideIcon,
  XCircle,
} from "lucide-react";

import {
  ApiError,
  api,
  type AuditEventRecord,
  type BackupResult,
  type ExportResult,
  type HealthPanel,
  type IntegrityReport,
  type MirrorResult,
  type OffsiteStatus,
} from "../../api";

/**
 * Trust — export, integrity, backup, and the audit log.
 *
 * This screen exists to make one question answerable without SSH: *could I get
 * my documents back?* Everything on it is a way of proving the answer rather
 * than asserting it — a hash check that can fail, an export you can open with
 * the stack stopped, a backup that refuses to run over corruption, and a full
 * record of every change anything ever made.
 *
 * The restore drill is deliberately not a button. It runs from a shell, against
 * a throwaway container, because the point of a drill is that it exercises the
 * path you would actually use at 2am — not a path that only exists inside the
 * app that just died.
 */
export default function TrustPage() {
  const [tab, setTab] = useState<"health" | "resilience" | "audit">("health");

  return (
    <div className="mx-auto max-w-5xl space-y-4">
      <PageHeader icon={ShieldCheck} title="Trust">
        Could you get your documents back? These are the ways to find out rather than
        assume.
      </PageHeader>

      <nav className="flex gap-1 border-b border-edge">
        {(
          [
            ["health", "Health"],
            ["resilience", "Export & resilience"],
            ["audit", "Audit log"],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            onClick={() => setTab(key)}
            className={
              tab === key
                ? "-mb-px border-b-2 border-accent px-3 py-2 text-sm font-medium"
                : "-mb-px border-b-2 border-transparent px-3 py-2 text-sm text-muted hover:text-neutral-100"
            }
          >
            {label}
          </button>
        ))}
      </nav>

      {tab === "health" ? (
        <HealthPanelView />
      ) : tab === "resilience" ? (
        <>
          <ResiliencePanel />
          <OffsitePanel />
        </>
      ) : (
        <AuditPanel />
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// Export, integrity, mirror, backup
// --------------------------------------------------------------------------

function ResiliencePanel() {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [integrity, setIntegrity] = useState<IntegrityReport | null>(null);
  const [exported, setExported] = useState<ExportResult | null>(null);
  const [goBag, setGoBag] = useState<ExportResult | null>(null);
  const [mirror, setMirror] = useState<MirrorResult | null>(null);
  const [backup, setBackup] = useState<BackupResult | null>(null);
  const [passphrase, setPassphrase] = useState("");

  async function run<T>(key: string, action: () => Promise<T>, then: (value: T) => void) {
    setBusy(key);
    setError(null);
    try {
      then(await action());
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-4">
      {error && (
        <p className="rounded-md border border-red-900 bg-red-950/40 p-3 text-sm text-red-300">
          {error}
        </p>
      )}

      <Card
        title="Integrity check"
        blurb="Re-reads every original and compares it against its own hash. Bit rot is
          silent; this is the only thing that notices. Run it before a backup, never after."
      >
        <Action
          busy={busy === "integrity"}
          label="Check every original"
          onClick={() => run("integrity", () => api.integrityCheck(), setIntegrity)}
        />
        {integrity && (
          <div className="mt-3 text-sm">
            {integrity.healthy ? (
              <p className="text-emerald-400">
                All {integrity.checked} originals match their hashes.
                {integrity.sealed?.length > 0 && (
                  <>
                    {" "}
                    <span className="text-muted">
                      {integrity.sealed.length} of them are in the vault — their
                      plaintext is gone on purpose, and their encrypted copies
                      are present.
                    </span>
                  </>
                )}
              </p>
            ) : (
              <div className="space-y-2 text-red-300">
                <p className="font-medium">
                  {integrity.corrupt.length} corrupt, {integrity.missing.length} missing.
                  Do not back up over this.
                </p>
                <ul className="space-y-1 font-mono text-xs">
                  {[...integrity.corrupt, ...integrity.missing].slice(0, 10).map((entry) => (
                    <li key={entry.sha256}>
                      {entry.original_filename ?? entry.sha256.slice(0, 12)}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {integrity.orphan_count > 0 && (
              <p className="mt-2 text-xs text-muted">
                {integrity.orphan_count} unreferenced files on disk. Listed, never
                removed — an orphan and an interrupted upload look identical from here.
              </p>
            )}
          </div>
        )}
      </Card>

      <Card
        title="Full export"
        blurb="Every original in a folder tree, plus an index.html you can open in any
          browser. It works with this whole stack stopped, which is the point: nothing
          you rely on should require the thing it is insurance against."
      >
        <Action
          busy={busy === "export"}
          label="Export the whole archive"
          onClick={() => run("export", () => api.exportFull(), setExported)}
        />
        {exported && (
          <p className="mt-3 text-sm text-neutral-200">
            {exported.document_count} documents in {exported.file_count} files →{" "}
            <code className="text-muted">{exported.path}</code>
            {exported.missing_blobs.length > 0 && (
              <span className="block text-red-300">
                {exported.missing_blobs.length} originals could not be found and are
                marked missing in the export.
              </span>
            )}
          </p>
        )}
      </Card>

      <Card
        title="Go-bag"
        blurb="Just the vital tier — birth certificate, DD-214, deed, passport —
          encrypted into a single zip small enough to carry. Encrypted because the
          whole point is that it leaves the house."
      >
        <div className="flex flex-wrap items-center gap-2">
          <input
            type="password"
            value={passphrase}
            onChange={(event) => setPassphrase(event.target.value)}
            placeholder="Passphrase (12+ characters)"
            className="w-72 rounded border border-field bg-ink px-2 py-1.5 text-sm"
          />
          <Action
            busy={busy === "gobag"}
            disabled={passphrase.length < 12}
            label="Build the go-bag"
            onClick={() =>
              run("gobag", () => api.exportGoBag(passphrase), (result) => {
                setGoBag(result);
                // It is not stored anywhere, so it should not linger here either.
                setPassphrase("");
              })
            }
          />
        </div>
        <p className="mt-2 text-xs text-muted">
          Write the passphrase down somewhere that is not this machine. Nothing here
          or on the server stores it, so a lost passphrase is a lost go-bag.
        </p>
        {goBag && (
          <p className="mt-2 text-sm text-neutral-200">
            {goBag.document_count} vital documents, encrypted →{" "}
            <code className="text-muted">{goBag.path}</code>
          </p>
        )}
      </Card>

      <Card
        title="Mirror tree"
        blurb="A live folder tree next to the blob store, browsable over the network
          without opening Bindery. Hardlinks, so it costs almost nothing, and it is
          safe to delete — the next rebuild recreates it exactly."
      >
        <Action
          busy={busy === "mirror"}
          label="Rebuild the mirror"
          onClick={() => run("mirror", () => api.rebuildMirror(), setMirror)}
        />
        {mirror && (
          <p className="mt-3 text-sm text-neutral-200">
            {mirror.linked} linked, {mirror.copied} copied, {mirror.bundles} bundles,{" "}
            {mirror.removed} stale entries cleared → <code>{mirror.root}</code>
          </p>
        )}
      </Card>

      <Card
        title="Backup"
        blurb="Integrity check, then the database, then the originals — in that order.
          Blobs are immutable and content-addressed, so a blob written after the dump
          is a harmless orphan; the other order would give you a dangling reference."
      >
        <Action
          busy={busy === "backup"}
          label="Run a backup now"
          onClick={() => run("backup", () => api.runBackup(), setBackup)}
        />
        {backup && (
          <p className="mt-3 text-sm text-neutral-200">
            {backup.blob_count} originals → <code>{backup.path}</code>
          </p>
        )}
        <div className="mt-4 rounded border border-edge bg-ink p-3">
          <p className="text-sm font-medium">The restore drill</p>
          <p className="mt-1 text-sm text-muted">
            A backup you have never restored is a story you tell yourself. This
            restores a generation into a clean, throwaway database, checks that every
            original it references is actually in the backup, and then searches the
            restored archive for the DD-214. It never touches the live stack.
          </p>
          <pre className="mt-2 overflow-x-auto rounded bg-black/60 p-2 text-xs text-neutral-200">
scripts/restore-drill.sh /data/backups/&lt;generation&gt;
          </pre>
          <p className="mt-1 text-xs text-muted">
            Deliberately not a button: a drill has to exercise the path you would use
            at 2am, not one that only exists inside the app that just died.
          </p>
        </div>
      </Card>
    </div>
  );
}

function Card({
  title,
  blurb,
  children,
}: {
  title: string;
  blurb: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-md border border-edge bg-surface p-4">
      <h2 className="text-sm font-semibold">{title}</h2>
      <p className="mb-3 mt-1 max-w-3xl text-sm text-muted">{blurb}</p>
      {children}
    </section>
  );
}

function Action({
  label,
  onClick,
  busy,
  disabled,
}: {
  label: string;
  onClick: () => void;
  busy: boolean;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy || disabled}
      className="rounded bg-accent px-3 py-1.5 text-sm font-medium text-ink disabled:opacity-40"
    >
      {busy ? "Working…" : label}
    </button>
  );
}

// --------------------------------------------------------------------------
// Offsite replication (T-13.7, REQ-164)
// --------------------------------------------------------------------------

/** "3 days ago", not a tick. A tick is a claim that stops being checked. */
function describeAge(seconds: number | null): string {
  if (seconds === null) return "never";
  if (seconds < 90) return "just now";
  if (seconds < 5400) return `${Math.round(seconds / 60)} minutes ago`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)} hours ago`;
  return `${Math.round(seconds / 86400)} days ago`;
}

function OffsitePanel() {
  const [status, setStatus] = useState<OffsiteStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    () => api.offsiteStatus().then(setStatus).catch(() => {}),
    [],
  );
  useEffect(() => {
    void load();
  }, [load]);

  // While a run is queued or in flight the worker owns it, so the only way to
  // learn it finished is to ask again.
  useEffect(() => {
    if (!status?.in_flight) return;
    const timer = setInterval(() => void load(), 5000);
    return () => clearInterval(timer);
  }, [status?.in_flight, load]);

  async function replicate() {
    setBusy(true);
    setError(null);
    try {
      setStatus(await api.offsiteReplicate());
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  if (!status) return null;

  return (
    <Card
      title="Offsite copy"
      blurb="The third copy, in S3, encrypted with a key you control. The other two are
        in this building on the same array — they survive a dead disk, not a fire."
    >
      {!status.configured ? (
        <p className="text-sm text-amber-400">
          Not configured. Nothing is leaving this machine — add the credentials in
          Settings.
        </p>
      ) : (
        <>
          <div className="flex flex-wrap items-baseline gap-2 text-sm">
            <span
              className={`h-2 w-2 rounded-full ${
                status.stale ? "bg-red-400" : "bg-emerald-400"
              }`}
            />
            <span className={status.stale ? "text-red-300" : ""}>
              Last successful copy {describeAge(status.last_success_age_seconds)}
            </span>
            {status.in_flight && (
              <span className="text-muted">· {status.in_flight} now</span>
            )}
          </div>

          {status.stale && (
            <p className="mt-1 text-sm text-red-300">
              {status.last_success_at
                ? "Two days is longer than the schedule allows — something is failing."
                : "No copy has ever left this machine."}
            </p>
          )}

          <div className="mt-3">
            <Action
              busy={busy}
              disabled={!!status.in_flight}
              label={status.in_flight ? "Queued" : "Replicate now"}
              onClick={() => void replicate()}
            />
          </div>
        </>
      )}

      {error && <p className="mt-2 text-sm text-red-300">{error}</p>}

      {status.runs.length > 0 && (
        <table className="mt-4 w-full text-left text-sm">
          <thead className="text-xs uppercase tracking-wide text-muted">
            <tr>
              <th className="py-1 font-medium">When</th>
              <th className="py-1 font-medium">Kind</th>
              <th className="py-1 font-medium">Result</th>
            </tr>
          </thead>
          <tbody>
            {status.runs.map((run) => (
              <tr key={run.id} className="border-t border-edge/60 align-top">
                <td className="whitespace-nowrap py-1.5 pr-3 text-muted">
                  {new Date(run.started_at).toLocaleString()}
                </td>
                <td className="py-1.5 pr-3 text-muted">
                  {run.kind}
                  {run.trigger === "manual" && " · asked for"}
                </td>
                <td className="py-1.5">
                  <span
                    className={
                      run.state === "succeeded"
                        ? "text-emerald-400"
                        : run.state === "failed"
                          ? "text-red-400"
                          : "text-muted"
                    }
                  >
                    {run.state}
                  </span>
                  {/* The reason, not just the verdict — a failure nobody can
                      diagnose from the screen sends you to the host logs. */}
                  {run.detail && <span className="text-muted"> — {run.detail}</span>}
                  {run.failures?.map((failure) => (
                    <div key={failure} className="text-xs text-red-300/80">
                      {failure}
                    </div>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

// --------------------------------------------------------------------------
// Audit log (T-6.8, REQ-069)
// --------------------------------------------------------------------------

const ACTOR_LABELS: Record<string, string> = {
  human: "you",
  ai: "Claude",
  rule: "a rule",
  system: "the system",
};

function AuditPanel() {
  const [events, setEvents] = useState<AuditEventRecord[]>([]);
  const [cursor, setCursor] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [actorType, setActorType] = useState("");
  const [entityId, setEntityId] = useState("");
  const [since, setSince] = useState("");

  const load = useCallback(
    async (append = false) => {
      setLoading(true);
      try {
        const page = await api.audit({
          actor_type: actorType || undefined,
          entity_id: entityId || undefined,
          since: since ? new Date(since).toISOString() : undefined,
          before_sequence: append && cursor ? cursor : undefined,
        });
        setEvents((current) => (append ? [...current, ...page.events] : page.events));
        setCursor(page.next_before_sequence);
      } finally {
        setLoading(false);
      }
      // `cursor` is intentionally excluded: including it would reload the first
      // page every time paging advanced it.
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [actorType, entityId, since],
  );

  useEffect(() => {
    // An async data load: the state is genuinely unavailable on the first
    // render, so the extra pass is the point rather than a mistake.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  return (
    <div className="space-y-3">
      <p className="text-sm text-muted">
        Every change anything ever made, newest first. Ordered by sequence rather than
        timestamp, because changes written together share a timestamp and only the
        sequence is a real order.
      </p>

      <div className="flex flex-wrap items-end gap-2">
        <Field label="Who">
          <select
            value={actorType}
            onChange={(event) => setActorType(event.target.value)}
            className="rounded border border-field bg-ink px-2 py-1.5 text-sm"
          >
            <option value="">Anyone</option>
            <option value="human">You</option>
            <option value="ai">Claude</option>
            <option value="rule">A rule</option>
            <option value="system">The system</option>
          </select>
        </Field>
        <Field label="Document">
          <input
            value={entityId}
            onChange={(event) => setEntityId(event.target.value.trim())}
            placeholder="document id"
            className="w-72 rounded border border-field bg-ink px-2 py-1.5 font-mono text-xs"
          />
        </Field>
        <Field label="Since">
          <input
            type="date"
            value={since}
            onChange={(event) => setSince(event.target.value)}
            className="rounded border border-field bg-ink px-2 py-1.5 text-sm"
          />
        </Field>
      </div>

      {loading && events.length === 0 ? (
        <p className="text-sm text-muted">Loading…</p>
      ) : events.length === 0 ? (
        <p className="rounded-md border border-edge bg-surface p-6 text-sm text-muted">
          Nothing matches those filters.
        </p>
      ) : (
        <ul className="divide-y divide-edge rounded-md border border-edge">
          {events.map((event) => (
            <li key={event.id} className="px-4 py-2.5 text-sm">
              <div className="flex flex-wrap items-baseline gap-x-2">
                <span className="font-mono text-xs text-muted">
                  {new Date(event.created_at).toLocaleString()}
                </span>
                <span className="font-medium">{event.action.replace(/_/g, " ")}</span>
                <span className="text-muted">
                  by {event.actor_label ?? ACTOR_LABELS[event.actor_type] ?? event.actor_type}
                </span>
                <span className="text-xs text-muted">{event.entity_type}</span>
              </div>
              {(event.before || event.after) && (
                <details className="mt-1">
                  <summary className="cursor-pointer text-xs text-muted">
                    what changed
                  </summary>
                  <pre className="mt-1 overflow-x-auto rounded bg-black/50 p-2 text-[11px] text-neutral-200">
{JSON.stringify({ before: event.before, after: event.after }, null, 2)}
                  </pre>
                </details>
              )}
            </li>
          ))}
        </ul>
      )}

      {cursor !== null && (
        <button
          type="button"
          onClick={() => void load(true)}
          disabled={loading}
          className="rounded border border-edge px-3 py-1.5 text-sm disabled:opacity-40"
        >
          {loading ? "Loading…" : "Load older"}
        </button>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs text-muted">{label}</span>
      {children}
    </label>
  );
}


// --------------------------------------------------------------------------
// Health (T-8.2, REQ-109)
// --------------------------------------------------------------------------

const STATE_LABELS: Record<string, string> = {
  received: "waiting to start",
  normalizing: "being converted",
  paging: "being read",
  segmenting: "being split",
  processed: "filed",
  failed: "failed",
  duplicate: "already had it",
};

/**
 * What the pipeline is doing, and whether anything is quietly broken.
 *
 * The ordering here is the argument: alerts first, then whether work is moving,
 * then what it costs. A number nobody has to act on is not the headline.
 */
function HealthPanelView() {
  const [panel, setPanel] = useState<HealthPanel | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setPanel(await api.healthPanel());
      setError(null);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useLiveQuery(["jobs", "files", "documents"], load, { fallbackMs: 60_000 });

  if (error) {
    return (
      <p role="alert" className="rounded-md border border-red-900 bg-red-950/40 p-3 text-sm text-red-300">
        {error}
      </p>
    );
  }
  if (loading && !panel) return <p className="text-sm text-muted">Loading…</p>;
  if (!panel) return null;

  const queued = Object.values(panel.queue_depth).reduce((total, n) => total + n, 0);

  return (
    <div className="space-y-4">
      {panel.alerts.length === 0 ? (
        <p className="rounded-md border border-emerald-900 bg-emerald-950/30 p-3 text-sm text-emerald-300">
          Everything is moving. {queued === 0 ? "Nothing is waiting." : `${queued} waiting.`}
        </p>
      ) : (
        <ul className="space-y-2">
          {panel.alerts.map((alert) => (
            <li
              key={alert.code}
              role={alert.severity === "critical" ? "alert" : undefined}
              className={
                alert.severity === "critical"
                  ? "rounded-md border border-red-900 bg-red-950/40 p-3 text-sm text-red-300"
                  : "rounded-md border border-amber-900 bg-amber-950/30 p-3 text-sm text-amber-300"
              }
            >
              {alert.message}
              {/* An alert with nothing to do about it trains people to
                  ignore alerts. Documents that gave up are re-runnable, and
                  the place to do it is one click away. */}
              {(alert.code === "dead_letter" || alert.code === "recent_failures") && (
                <Link
                  to="/pipeline"
                  className="ml-2 underline underline-offset-2"
                >
                  Review and re-run them
                </Link>
              )}
            </li>
          ))}
        </ul>
      )}

      <div className="grid gap-3 sm:grid-cols-4">
        <Stat label="Waiting" value={queued} icon={Clock} />
        <Stat label="Running" value={panel.running} icon={Loader} />
        {/* Colour only when the number means something. A zero here is the
            good outcome and should look like every other calm number; it is
            the non-zero one that has to catch your eye. */}
        <Stat label="Failed today" value={panel.failed_24h} icon={AlertTriangle} tone="warn" />
        <Stat label="Gave up" value={panel.dead_letter} icon={XCircle} tone="bad" />
      </div>

      <Card
        title="Files"
        blurb="Where everything that has arrived currently sits. Nothing here is ever
          discarded — a file that failed to process is still stored, byte for byte."
      >
        <ul className="space-y-1 text-sm">
          {Object.entries(panel.files_by_state).map(([state, count]) => (
            <li key={state} className="flex justify-between">
              <span>{STATE_LABELS[state] ?? state}</span>
              <span className="text-muted">{count}</span>
            </li>
          ))}
        </ul>
      </Card>

      <Card
        title="API spend, last 30 days"
        blurb="Approximate, from recorded token usage. It is here to catch a runaway
          loop early — a bug that presents as a bill."
      >
        <p className="text-2xl font-semibold">${panel.spend_30d_usd.toFixed(2)}</p>
        {panel.spend_by_day.length > 0 && (
          <ul className="mt-3 space-y-0.5 text-xs text-muted">
            {panel.spend_by_day.slice(-7).map((day) => (
              <li key={day.day} className="flex justify-between">
                <span>{day.day}</span>
                <span>${day.usd.toFixed(2)}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>

      {panel.stuck_jobs.length > 0 && (
        <Card
          title="Stuck"
          blurb="These claimed a slot and never released it — usually a worker that
            died. They are recoverable; nothing has been lost."
        >
          <ul className="space-y-1 text-sm">
            {panel.stuck_jobs.map((job) => (
              <li key={job.id} className="flex justify-between">
                <span>{job.stage}</span>
                <span className="text-muted">
                  {job.locked_by} ·{" "}
                  {Math.round((job.locked_for_seconds ?? 0) / 60)} min
                </span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <p className="text-xs text-muted">
        Checked {new Date(panel.checked_at).toLocaleTimeString()}. The worker also
        checks on its own and can push a notification when the pipeline stops — set a
        webhook in Settings.
      </p>
    </div>
  );
}

function Stat({
  label,
  value,
  icon: Icon,
  tone,
}: {
  label: string;
  value: number;
  icon: LucideIcon;
  tone?: "warn" | "bad";
}) {
  const loud = value > 0 && tone;
  return (
    <div
      className={`rounded-lg border p-3 ${
        loud === "bad"
          ? "border-red-900/70 bg-red-950/25"
          : loud === "warn"
            ? "border-amber-900/70 bg-amber-950/20"
            : "border-edge bg-surface"
      }`}
    >
      <p className="flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-muted">
        <Icon
          size={12}
          className={
            loud === "bad" ? "text-red-400" : loud === "warn" ? "text-amber-400" : ""
          }
        />
        {label}
      </p>
      <p
        className={`mt-1 text-2xl font-semibold ${
          loud === "bad" ? "text-red-300" : loud === "warn" ? "text-amber-300" : ""
        }`}
      >
        {value}
      </p>
    </div>
  );
}
