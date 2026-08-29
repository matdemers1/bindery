import { Tags } from "lucide-react";

import PageHeader from "../../components/PageHeader";
import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";

import {
  ApiError,
  api,
  type AssetRef,
  type CorrespondentRef,
  type DuplicatePair,
  type MergePreview,
  type TaxonomyHealth,
  type UnifyProposal,
  type TimelineEntry,
} from "../../api";

/**
 * Organise (Phase 5) — correspondents, assets, and taxonomy health.
 *
 * One screen with three tabs rather than three screens, because they are the
 * same activity: tidying the vocabulary the archive files things under. Merge is
 * available from every problem this surfaces — showing a problem with no tool to
 * fix it is worse than not showing it.
 */
const TABS = [
  { key: "correspondents", label: "From" },
  { key: "assets", label: "Things" },
  { key: "taxonomy", label: "Taxonomy" },
];

export default function OrganisePage({ libraries }: { libraries: { id: string }[] }) {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "correspondents";

  return (
    <div className="mx-auto max-w-5xl">
      <PageHeader icon={Tags} title="Organise">
        The vocabulary the archive files things under. Near-duplicates here are what
        make documents unfindable under the name you&apos;d actually reach for.
      </PageHeader>

      <div className="mb-5 flex gap-1">
        {TABS.map((option) => (
          <button
            key={option.key}
            onClick={() => setParams({ tab: option.key }, { replace: true })}
            className={`rounded-md px-3 py-1.5 text-sm ${
              tab === option.key ? "bg-surface text-neutral-100" : "text-muted hover:text-neutral-200"
            }`}
          >
            {option.label}
          </button>
        ))}
      </div>

      {tab === "correspondents" && <Correspondents />}
      {tab === "assets" && <Assets libraryId={libraries[0]?.id} />}
      {tab === "taxonomy" && (
        <>
          <UnifyPass />
          <Taxonomy />
        </>
      )}
    </div>
  );
}

/** Merge, with the preview and the undo attached to the same control. */
function MergeControl({
  options,
  onMerge,
  onPreview,
  noun,
}: {
  options: { id: string; name: string }[];
  onMerge: (sourceId: string, targetId: string) => Promise<MergePreview>;
  onPreview?: (sourceId: string, targetId: string) => Promise<MergePreview>;
  noun: string;
}) {
  const [source, setSource] = useState("");
  const [target, setTarget] = useState("");
  const [preview, setPreview] = useState<MergePreview | null>(null);
  const [done, setDone] = useState<MergePreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const ready = source && target && source !== target;

  return (
    <div className="rounded-lg border border-edge bg-surface p-4">
      <p className="mb-2 text-xs tracking-wide text-muted uppercase">Merge {noun}</p>
      <div className="flex flex-wrap items-center gap-2">
        <Select value={source} onChange={setSource} options={options} placeholder="Merge this…" />
        <span className="text-muted">into</span>
        <Select value={target} onChange={setTarget} options={options} placeholder="…this" />

        {onPreview && (
          <button
            onClick={async () => {
              setBusy(true); setError(null);
              try { setPreview(await onPreview(source, target)); }
              catch (e) { setError(e instanceof ApiError ? e.message : "Could not preview."); }
              finally { setBusy(false); }
            }}
            disabled={!ready || busy}
            className="rounded-md border border-edge px-3 py-1.5 text-sm disabled:opacity-40"
          >
            Preview
          </button>
        )}
        <button
          onClick={async () => {
            setBusy(true); setError(null);
            try {
              setDone(await onMerge(source, target));
              setPreview(null); setSource(""); setTarget("");
            } catch (e) { setError(e instanceof ApiError ? e.message : "Could not merge."); }
            finally { setBusy(false); }
          }}
          disabled={!ready || busy || (Boolean(onPreview) && !preview)}
          title={onPreview && !preview ? "Preview it first" : undefined}
          className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-ink disabled:opacity-40"
        >
          Merge
        </button>
      </div>

      {error && <p className="mt-2 text-sm text-red-400">{error}</p>}

      {preview && (
        <p className="mt-2 text-sm text-muted">
          Would move <strong className="text-neutral-200">{preview.document_count}</strong>{" "}
          documents from “{preview.from_name}” into “{preview.into_name}”, and keep the old
          name as an alias. Nothing has been written.
        </p>
      )}

      {done && (
        <p className="mt-2 flex flex-wrap items-center gap-3 text-sm text-accent">
          Merged “{done.from_name}” into “{done.into_name}”.
          {done.operation_id && (
            <button
              onClick={async () => {
                await api.undoMerge(done.operation_id!);
                setDone(null);
                location.reload();
              }}
              className="rounded border border-edge px-2 py-0.5 text-xs text-muted"
            >
              Undo
            </button>
          )}
        </p>
      )}
    </div>
  );
}

function Select({
  value, onChange, options, placeholder,
}: {
  value: string;
  onChange: (value: string) => void;
  options: { id: string; name: string }[];
  placeholder: string;
}) {
  return (
    <select
      value={value}
      onChange={(event) => onChange(event.target.value)}
      className="min-w-0 flex-1 rounded-md border border-edge bg-ink px-2 py-1.5 text-sm"
    >
      <option value="">{placeholder}</option>
      {options.map((option) => (
        <option key={option.id} value={option.id}>{option.name}</option>
      ))}
    </select>
  );
}

function Correspondents() {
  const [rows, setRows] = useState<CorrespondentRef[]>([]);
  const load = useCallback(() => api.correspondents().then(setRows).catch(() => {}), []);
  useEffect(() => { void load(); }, [load]);

  return (
    <div className="space-y-4">
      <MergeControl
        noun="correspondents"
        options={rows}
        onPreview={api.previewMerge}
        onMerge={async (a, b) => {
          const result = await api.mergeCorrespondents(a, b);
          await load();
          return result;
        }}
      />

      {rows.length === 0 ? (
        <Empty>No correspondents yet. They appear as documents are classified.</Empty>
      ) : (
        <ul className="divide-y divide-edge rounded-lg border border-edge">
          {rows.map((row) => (
            <li key={row.id} className="p-3">
              <p className="flex items-baseline justify-between gap-3">
                <span className="font-medium">{row.name}</span>
                <span className="text-xs text-muted">
                  {row.document_count} {row.document_count === 1 ? "document" : "documents"}
                </span>
              </p>
              {row.aliases.length > 0 && (
                <p className="mt-1 text-xs text-muted">
                  also known as {row.aliases.join(", ")}
                </p>
              )}
              <AliasForm correspondentId={row.id} onAdded={load} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function AliasForm({ correspondentId, onAdded }: { correspondentId: string; onAdded: () => void }) {
  const [alias, setAlias] = useState("");
  return (
    <form
      className="mt-2 flex gap-2"
      onSubmit={async (event) => {
        event.preventDefault();
        if (!alias.trim()) return;
        await api.addAlias(correspondentId, alias.trim());
        setAlias("");
        onAdded();
      }}
    >
      <input
        value={alias}
        onChange={(event) => setAlias(event.target.value)}
        placeholder="Add another spelling…"
        className="min-w-0 flex-1 rounded border border-edge bg-ink px-2 py-1 text-xs outline-none focus:border-accent"
      />
      <button type="submit" className="rounded border border-edge px-2 py-1 text-xs text-muted">
        Add
      </button>
    </form>
  );
}

function Assets({ libraryId }: { libraryId?: string }) {
  const [rows, setRows] = useState<AssetRef[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [timeline, setTimeline] = useState<TimelineEntry[]>([]);
  const [name, setName] = useState("");
  const [kind, setKind] = useState("vehicle");

  const load = useCallback(() => api.assets().then(setRows).catch(() => {}), []);
  useEffect(() => { void load(); }, [load]);

  return (
    <div className="space-y-4">
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          if (!libraryId || !name.trim()) return;
          await api.createAsset({
            library_id: libraryId, kind, name: name.trim(), attributes: {},
          });
          setName("");
          await load();
        }}
        className="flex flex-wrap gap-2 rounded-lg border border-edge bg-surface p-4"
      >
        <select
          value={kind}
          onChange={(event) => setKind(event.target.value)}
          className="rounded-md border border-edge bg-ink px-2 py-1.5 text-sm"
        >
          {["vehicle", "property", "policy", "account", "person", "other"].map((k) => (
            <option key={k} value={k}>{k}</option>
          ))}
        </select>
        <input
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="2020 Honda Accord"
          className="min-w-0 flex-1 rounded-md border border-edge bg-ink px-3 py-1.5 text-sm outline-none focus:border-accent"
        />
        <button
          type="submit"
          className="rounded-md border border-edge px-3 py-1.5 text-sm hover:border-accent/60"
        >
          Add
        </button>
      </form>

      {rows.length === 0 ? (
        <Empty>
          No things yet. An asset is what documents are <em>about</em> — a car, the
          house, a policy — and it's what makes "everything about the Honda" a
          single view instead of a search.
        </Empty>
      ) : (
        <ul className="space-y-2">
          {rows.map((asset) => (
            <li key={asset.id} className="rounded-lg border border-edge bg-surface p-3">
              <button
                onClick={async () => {
                  if (open === asset.id) return setOpen(null);
                  setOpen(asset.id);
                  setTimeline((await api.assetTimeline(asset.id)).entries);
                }}
                className="flex w-full items-baseline justify-between gap-3 text-left"
              >
                <span>
                  <span className="font-medium">{asset.name}</span>
                  <span className="ml-2 rounded-full border border-edge px-2 text-xs text-muted">
                    {asset.kind}
                  </span>
                </span>
                <span className="text-xs text-muted">
                  {asset.document_count} · {open === asset.id ? "hide" : "timeline"}
                </span>
              </button>

              {open === asset.id && (
                <ol className="mt-3 space-y-2 border-l border-edge pl-4">
                  {timeline.length === 0 && (
                    <li className="text-sm text-muted">
                      Nothing attached yet. Open a document and attach it here.
                    </li>
                  )}
                  {timeline.map((entry) => (
                    <li key={entry.document_id} className="relative">
                      <span className="absolute -left-[21px] top-1.5 h-2 w-2 rounded-full bg-accent" />
                      <Link
                        to={`/document/${entry.document_id}/page/1`}
                        className="flex items-baseline gap-3 hover:underline"
                      >
                        <span className="font-mono text-xs text-muted">{entry.date}</span>
                        <span className="min-w-0 truncate text-sm">
                          {entry.title ?? "(untitled)"}
                        </span>
                      </Link>
                      <p className="ml-[4.2rem] text-xs text-muted">
                        {entry.correspondent ?? "unknown sender"}
                        {!entry.dated_precisely && " · dated by arrival"}
                      </p>
                    </li>
                  ))}
                </ol>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Taxonomy() {
  const [health, setHealth] = useState<TaxonomyHealth | null>(null);
  const [duplicates, setDuplicates] = useState<DuplicatePair[]>([]);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setHealth(await api.taxonomyHealth());
    setDuplicates(await api.duplicates());
  }, []);
  useEffect(() => { void load(); }, [load]);

  if (!health) return <Empty>Loading…</Empty>;

  return (
    <div className="space-y-4">
      <section className="rounded-lg border border-edge bg-surface p-4">
        <dl className="grid grid-cols-3 gap-4 text-sm">
          <div><dt className="text-xs text-muted">Tags</dt><dd className="font-mono text-lg">{health.total_tags}</dd></div>
          <div><dt className="text-xs text-muted">Used once</dt><dd className="font-mono text-lg">{health.used_once}</dd></div>
          <div><dt className="text-xs text-muted">Never used</dt><dd className="font-mono text-lg">{health.unused}</dd></div>
        </dl>
        {health.exceeds_alarm && (
          <p className="mt-3 rounded-md border border-amber-500/40 p-3 text-sm text-amber-300">
            {Math.round(health.orphan_ratio * 100)}% of tags are used exactly once. Past
            15% the plan treats that as the taxonomy drifting despite the reuse
            contract — worth merging the near-duplicates below and tightening the
            prompt.
          </p>
        )}
      </section>

      {health.near_duplicate_tags.length > 0 && (
        <section className="rounded-lg border border-edge bg-surface p-4">
          <p className="mb-2 text-xs tracking-wide text-muted uppercase">
            Tags that look like the same thing
          </p>
          <ul className="space-y-1">
            {health.near_duplicate_tags.map((pair) => (
              <li key={`${pair.a_id}-${pair.b_id}`} className="flex items-center gap-3 text-sm">
                <span className="flex-1">
                  {pair.a_name} <span className="text-muted">vs</span> {pair.b_name}
                  <span className="ml-2 text-xs text-muted">
                    {Math.round(pair.similarity * 100)}% alike
                  </span>
                </span>
                <button
                  onClick={async () => {
                    await api.mergeTags(pair.a_id, pair.b_id);
                    await load();
                  }}
                  className="rounded border border-edge px-2 py-0.5 text-xs"
                >
                  Merge →
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="rounded-lg border border-edge bg-surface p-4">
        <div className="mb-2 flex items-center justify-between">
          <p className="text-xs tracking-wide text-muted uppercase">Possible duplicates</p>
          <button
            onClick={async () => {
              setBusy(true);
              try { await api.scanDuplicates(); await load(); } finally { setBusy(false); }
            }}
            disabled={busy}
            className="rounded border border-edge px-2 py-1 text-xs disabled:opacity-40"
          >
            {busy ? "Scanning…" : "Scan"}
          </button>
        </div>
        {duplicates.length === 0 ? (
          <p className="text-sm text-muted">None found.</p>
        ) : (
          <ul className="space-y-2 text-sm">
            {duplicates.map((pair) => (
              <li key={pair.id} className="flex flex-wrap items-center gap-2">
                <Link to={`/document/${pair.document_a_id}/page/1`} className="underline underline-offset-2">
                  {pair.a_title ?? "(untitled)"}
                </Link>
                <span className="text-muted">and</span>
                <Link to={`/document/${pair.document_b_id}/page/1`} className="underline underline-offset-2">
                  {pair.b_title ?? "(untitled)"}
                </Link>
                <span className="text-xs text-muted">
                  {Math.round(pair.similarity * 100)}% alike
                </span>
              </li>
            ))}
          </ul>
        )}
        <p className="mt-2 text-xs text-muted">
          Recorded, never resolved. Two scans of one deed at different qualities are
          both worth keeping until you decide otherwise.
        </p>
      </section>
    </div>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <p className="rounded-lg border border-edge p-8 text-center text-sm text-muted">{children}</p>
  );
}


/**
 * Asking which folders are the same organisation.
 *
 * Trigram similarity — the near-duplicate list below — is the right tool for a
 * typo, and it finds "26th Weapon School" next to "26th Weapons School" every
 * time. What it cannot know is whether a squadron and its school are one
 * organisation or two, because that is knowledge about the world rather than
 * about the strings.
 *
 * So this asks, and then does nothing until you agree. Only the names are sent
 * — never document text — so it costs almost nothing and nothing about the
 * contents of the archive leaves it. Each group is applied as ordinary merges,
 * which are audited and undoable one at a time.
 */
function UnifyPass() {
  const [proposal, setProposal] = useState<UnifyProposal | null>(null);
  const [busy, setBusy] = useState(false);
  const [applying, setApplying] = useState<string | null>(null);
  const [applied, setApplied] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  async function propose() {
    setBusy(true);
    setError(null);
    try {
      setProposal(await api.unifyPreview());
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  async function apply(group: UnifyProposal["groups"][number]) {
    if (!group.canonical_id) return;
    setApplying(group.canonical_id);
    try {
      await api.unifyApply(
        group.canonical_id,
        group.members.map((m) => m.id).filter((id) => id !== group.canonical_id),
      );
      setApplied((current) => new Set(current).add(group.canonical_id!));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setApplying(null);
    }
  }

  return (
    <section className="mb-5 rounded-xl border border-edge bg-surface p-4">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-sm font-medium">Unify folders</h2>
        <span className="flex-1" />
        <button
          type="button"
          onClick={() => void propose()}
          disabled={busy}
          className="rounded bg-accent px-3 py-1.5 text-sm font-medium text-ink disabled:opacity-40"
        >
          {busy ? "Looking…" : "Look for folders that are the same thing"}
        </button>
      </div>
      <p className="mt-1 max-w-2xl text-sm text-muted">
        Twenty years of letterheads spell one unit four ways. This reads the list of
        names — never the documents — and proposes which ones are the same
        organisation. Nothing merges until you say so, and every merge can be undone.
      </p>

      {error && (
        <p role="alert" className="mt-3 rounded border border-red-900 bg-red-950/40 p-2.5 text-sm text-red-300">
          {error}
        </p>
      )}

      {proposal?.unavailable_reason && (
        <p className="mt-3 rounded border border-edge bg-ink p-2.5 text-sm text-muted">
          {proposal.unavailable_reason}
        </p>
      )}

      {proposal && !proposal.unavailable_reason && proposal.groups.length === 0 && (
        <p className="mt-3 text-sm text-muted">
          Nothing worth merging among {proposal.considered} names.
        </p>
      )}

      {proposal && proposal.groups.length > 0 && (
        <ul className="mt-3 space-y-2">
          {proposal.groups.map((group) => {
            const done = group.canonical_id ? applied.has(group.canonical_id) : false;
            return (
              <li
                key={group.canonical_id ?? group.canonical}
                className={`rounded-lg border p-3 ${
                  done ? "border-emerald-900/60 bg-emerald-950/20" : "border-edge bg-ink"
                }`}
              >
                <div className="flex flex-wrap items-baseline gap-2">
                  <span className="text-sm font-medium">{group.canonical}</span>
                  <span className="text-xs text-muted">
                    {group.members.length} names · {group.document_count} documents
                  </span>
                  <span className="flex-1" />
                  {done ? (
                    <span className="text-xs text-emerald-400">merged</span>
                  ) : (
                    <button
                      type="button"
                      onClick={() => void apply(group)}
                      disabled={applying !== null}
                      className="rounded border border-edge px-2.5 py-1 text-xs hover:border-accent/60 disabled:opacity-40"
                    >
                      {applying === group.canonical_id ? "Merging…" : "Merge these"}
                    </button>
                  )}
                </div>
                {group.reason && (
                  <p className="mt-1 text-xs text-muted">{group.reason}</p>
                )}
                <ul className="mt-2 flex flex-wrap gap-1.5">
                  {group.members.map((member) => (
                    <li
                      key={member.id}
                      className={`rounded-full border px-2 py-0.5 text-[11px] ${
                        member.id === group.canonical_id
                          ? "border-accent/60 text-accent"
                          : "border-edge text-muted"
                      }`}
                    >
                      {member.name}
                      <span className="ml-1 opacity-60">{member.documents}</span>
                    </li>
                  ))}
                </ul>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
