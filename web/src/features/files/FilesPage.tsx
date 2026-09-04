import { FileText, Folder, FolderTree, Layers, ShieldCheck } from "lucide-react";

import PageHeader from "../../components/PageHeader";
import { useCallback, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router";

import { ApiError, api, type FileTreeNode, type LibraryDetail, type MovePlan } from "../../api";
import { useLiveQuery } from "../../live/LiveProvider";
import { Badge, Button } from "@d3cloud/ui";

/**
 * The folder tree, in the app.
 *
 * Search answers "where is the thing I know I have". The archive browser
 * answers "what came in recently". Neither answers the plainest question of
 * all — *what is actually in here?* — the way a folder does. So this is a
 * folder: correspondent, then year, then documents, walked one level at a time.
 *
 * It is the same tree the mirror writes to disk and the export ships, computed
 * from the database rather than read off the filesystem, so it is correct even
 * when the mirror hasn't been rebuilt. What you browse here is what you get on
 * a USB stick.
 *
 * Bundles are the interesting case and are deliberately *not* flattened into
 * one row per document: a twelve-page scan holding three documents is one file
 * on disk, because splitting it would mean modifying an original. The row says
 * so and links to the pages.
 */
const INGEST_LABELS: Record<string, string> = {
  watched_folder: "scanner",
  web_upload: "uploaded",
  camera: "camera",
  bulk_import: "imported",
};

export default function FilesPage() {
  const [params, setParams] = useSearchParams();
  const [tree, setTree] = useState<FileTreeNode[]>([]);
  const [libraries, setLibraries] = useState<LibraryDetail[]>([]);
  const [loading, setLoading] = useState(true);

  const path = params.get("path") ?? "";

  // What the tree is currently showing. A background refresh must not replace
  // the folder with a spinner: the pipeline pushes a hint for every file it
  // touches, so during an import this screen would flash empty several times a
  // second. Opening a *different* folder is the case that has genuinely
  // nothing to show yet, and that is the one the loading state is for.
  const showing = useRef<string | null>(null);

  const load = useCallback(async () => {
    if (showing.current !== path) setLoading(true);
    try {
      const [result, held] = await Promise.all([
        api.fileTree(path),
        api.householdLibraries(),
      ]);
      setTree(result.nodes);
      setLibraries(held);
      showing.current = path;
    } finally {
      setLoading(false);
    }
  }, [path]);

  // The tree is what the pipeline is writing into, so it goes stale exactly
  // when files move and documents are filed. `key` is the folder: the topics
  // say when to refetch, and the path says what to refetch.
  useLiveQuery(["files", "documents"], load, { key: path });

  function go(next: string) {
    const search = new URLSearchParams();
    if (next) search.set("path", next);
    setParams(search);
  }

  const segments = path ? path.split("/") : [];

  return (
    <div className="mx-auto max-w-5xl space-y-4">
      <PageHeader icon={FolderTree} title="Files">
        The archive as a folder tree — the same one the mirror writes to disk and the
        export ships. Every row says how the file arrived and what it is tagged with.
      </PageHeader>

      <nav className="flex flex-wrap items-center gap-1 text-sm">
        <Crumb onClick={() => go("")} active={segments.length === 0}>
          Archive
        </Crumb>
        {segments.map((segment, index) => (
          <span key={segment + index} className="flex items-center gap-1">
            <span className="text-fg-faint">/</span>
            <Crumb
              onClick={() => go(segments.slice(0, index + 1).join("/"))}
              active={index === segments.length - 1}
            >
              {segment}
            </Crumb>
          </span>
        ))}
      </nav>

      {loading ? (
        <p className="text-sm text-muted">Loading…</p>
      ) : tree.length === 0 ? (
        <p className="rounded-md border border-edge bg-surface p-6 text-sm text-muted">
          Nothing here yet. Documents appear once they have been filed — until then
          they are on the <Link className="underline" to="/review">Review</Link> screen.
        </p>
      ) : (
        <ul className="divide-y divide-edge rounded-md border border-edge">
          {tree.map((node) =>
            node.kind === "folder" ? (
              <FolderRow key={node.path} node={node} onOpen={() => go(node.path)} />
            ) : (
              <DocumentRow
                key={node.path + node.document_id}
                node={node}
                libraries={libraries}
                onMoved={() => void load()}
              />
            ),
          )}
        </ul>
      )}
    </div>
  );
}

function Crumb({
  children,
  onClick,
  active,
}: {
  children: React.ReactNode;
  onClick: () => void;
  active: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        active
          ? "rounded px-1.5 py-0.5 font-medium text-fg"
          : "rounded px-1.5 py-0.5 text-muted hover:bg-edge hover:text-fg"
      }
    >
      {children}
    </button>
  );
}

function FolderRow({ node, onOpen }: { node: FileTreeNode; onOpen: () => void }) {
  return (
    <li>
      <button
        type="button"
        onClick={onOpen}
        className="flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-surface"
      >
        {node.name === "_bundles" ? (
          <Layers size={16} className="shrink-0 text-accent" aria-hidden />
        ) : (
          <Folder size={16} className="shrink-0 text-accent" aria-hidden />
        )}
        <span className="flex-1 font-medium">
          {node.name === "_bundles" ? "Multi-document scans" : node.name}
        </span>
        <span className="text-xs text-muted">
          {node.child_count} {node.child_count === 1 ? "item" : "items"}
        </span>
      </button>
    </li>
  );
}

function DocumentRow({
  node,
  libraries,
  onMoved,
}: {
  node: FileTreeNode;
  libraries: LibraryDetail[];
  onMoved: () => void;
}) {
  const bundle = node.kind === "bundle";
  const target =
    node.document_id && node.page_start
      ? `/document/${node.document_id}/page/${node.page_start}`
      : "/";

  return (
    <li className="px-4 py-3 hover:bg-surface">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        {bundle ? (
          <Layers size={15} className="shrink-0 text-muted" aria-hidden />
        ) : (
          <FileText size={15} className="shrink-0 text-muted" aria-hidden />
        )}
        <Link to={target} className="font-medium underline-offset-2 hover:underline">
          {node.title ?? node.original_filename ?? node.name}
        </Link>
        {node.sensitivity === "vital" && (
          <Badge tone="attention" size="sm">
            <ShieldCheck size={11} aria-hidden />
            vital
          </Badge>
        )}
        {node.document_date && (
          <span className="text-xs text-muted">{node.document_date}</span>
        )}
      </div>

      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
        {node.ingest_source && (
          <span title="How this file arrived">
            {INGEST_LABELS[node.ingest_source] ?? node.ingest_source}
            {node.received_at ? ` ${node.received_at.slice(0, 10)}` : ""}
          </span>
        )}
        {node.document_type && <span>{node.document_type}</span>}
        {bundle ? (
          <span className="text-fg">
            one scan of {node.page_count} pages, holding several documents — not split,
            because originals are never modified
          </span>
        ) : (
          node.page_start != null &&
          node.page_end != null && (
            <span>
              {node.page_start === node.page_end
                ? `p. ${node.page_start}`
                : `pp. ${node.page_start}–${node.page_end}`}
            </span>
          )
        )}
        {node.review_state === "needs_review" && (
          <Link to="/review" className="text-warning underline">
            needs review
          </Link>
        )}
        {libraries.length > 1 && node.library_name && (
          <span title="Which library this is in">in {node.library_name}</span>
        )}
      </div>

      {libraries.length > 1 && node.source_file_id && node.library_id && (
        <MoveControl
          sourceFileId={node.source_file_id}
          currentLibraryId={node.library_id}
          libraries={libraries}
          onMoved={onMoved}
        />
      )}

      {node.tags.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1">
          {node.tags.map((tag) => (
            <span
              key={tag}
              className="rounded bg-edge px-1.5 py-0.5 text-11 text-fg"
            >
              {tag}
            </span>
          ))}
        </div>
      )}
    </li>
  );
}


/**
 * Moving a file to another library.
 *
 * Always previewed, never one click. Cross-library search is impossible by
 * design, so this makes a set of documents vanish from one person's view and
 * appear in another's — and tags and correspondents belong to a library, so
 * some of them will not survive the trip. The preview says which, by name,
 * before anything happens.
 *
 * Only libraries you can write to are offered: moving into a library you only
 * read would be a way to put documents somewhere you cannot be held to account
 * for putting them.
 */
function MoveControl({
  sourceFileId,
  currentLibraryId,
  libraries,
  onMoved,
}: {
  sourceFileId: string;
  currentLibraryId: string;
  libraries: LibraryDetail[];
  onMoved: () => void;
}) {
  const [target, setTarget] = useState("");
  const [plan, setPlan] = useState<MovePlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const destinations = libraries.filter(
    (library) => library.id !== currentLibraryId && library.your_role !== "reader",
  );
  if (destinations.length === 0) return null;

  async function run(action: () => Promise<MovePlan>, done?: () => void) {
    setBusy(true);
    setError(null);
    try {
      setPlan(await action());
      done?.();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-2 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <select
          aria-label="Move this file to another library"
          value={target}
          onChange={(event) => {
            setTarget(event.target.value);
            setPlan(null);
          }}
          className="rounded border border-field bg-ink px-1.5 py-1"
        >
          <option value="">Move to…</option>
          {destinations.map((library) => (
            <option key={library.id} value={library.id}>
              {library.name}
            </option>
          ))}
        </select>
        {target && !plan && (
          <Button size="sm" disabled={busy} onClick={() => void run(() => api.previewMove(sourceFileId, target))}>
            {busy ? "Checking…" : "Preview"}
          </Button>
        )}
      </div>

      {error && <p className="mt-1 text-danger">{error}</p>}

      {plan && (
        <div className="mt-2 rounded border border-edge bg-ink p-2">
          <p>
            Moves {plan.document_count}{" "}
            {plan.document_count === 1 ? "document" : "documents"}.
          </p>
          {plan.loses_metadata ? (
            <p className="mt-1 text-warning">
              These belong to the old library and will be cleared:{" "}
              {[
                ...plan.cleared_tags,
                ...plan.cleared_correspondents,
                ...plan.cleared_types,
              ].join(", ")}
              . The change is recorded in the audit log, so you can see what was lost.
            </p>
          ) : (
            <p className="mt-1 text-muted">Nothing is lost in the move.</p>
          )}
          <div className="mt-2 flex gap-2">
            <Button variant="primary" size="sm" disabled={busy} onClick={() => void run( () => api.moveFile(sourceFileId, target), () => { setPlan(null); setTarget(""); onMoved(); }, ) }>
              {busy ? "Moving…" : "Move"}
            </Button>
            <Button size="sm" onClick={() => setPlan(null)}>
              Cancel
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
