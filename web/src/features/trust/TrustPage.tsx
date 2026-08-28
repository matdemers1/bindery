import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  api,
  type AuditEventRecord,
  type BackupResult,
  type ExportResult,
  type IntegrityReport,
  type MirrorResult,
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
  const [tab, setTab] = useState<"resilience" | "audit">("resilience");

  return (
    <div className="space-y-4">
      <header className="space-y-1">
        <h1 className="text-lg font-semibold">Trust</h1>
        <p className="text-sm text-muted">
          Could you get your documents back? These are the ways to find out rather
          than assume.
        </p>
      </header>

      <nav className="flex gap-1 border-b border-edge">
        {(
          [
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

      {tab === "resilience" ? <ResiliencePanel /> : <AuditPanel />}
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
            className="w-72 rounded border border-edge bg-ink px-2 py-1.5 text-sm"
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
      // eslint-disable-next-line react-hooks/exhaustive-deps
    },
    [actorType, entityId, since],
  );

  useEffect(() => {
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
            className="rounded border border-edge bg-ink px-2 py-1.5 text-sm"
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
            className="w-72 rounded border border-edge bg-ink px-2 py-1.5 font-mono text-xs"
          />
        </Field>
        <Field label="Since">
          <input
            type="date"
            value={since}
            onChange={(event) => setSince(event.target.value)}
            className="rounded border border-edge bg-ink px-2 py-1.5 text-sm"
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
