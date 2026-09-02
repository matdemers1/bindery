import { useCallback, useMemo, useRef, useState } from "react";
import {
  FileText,
  Plus,
  Trash2,
  Upload,
  UploadCloud,
} from "lucide-react";

import { ApiError, api, type FileProgress, type Library } from "../../api";
import { useLiveQuery } from "../../live/LiveProvider";
import LogViewer from "../../components/LogViewer";
import PageHeader from "../../components/PageHeader";
import PipelineFlow, { FileRow } from "./PipelineFlow";

/**
 * Adding files.
 *
 * Previously this was a button that opened the OS file dialog, and everything
 * after that happened somewhere you could not see: the dialog closed, a toast
 * said a number, and the files went off to be processed with no way to watch or
 * to find out what became of them.
 *
 * So this is a page with three phases, and the middle one is the point:
 *
 *   1. **Stage.** Drop or pick files and *look at them* before committing.
 *      Removing the wrong scan before it enters an archive that never deletes
 *      anything is worth a click.
 *   2. **Watch.** The pipeline drawn as its actual stages, with every file
 *      visible at the one it is sitting on.
 *   3. **Explain.** A log, per file, for when a file stops somewhere.
 */
const MAX_STAGED = 200;

type Staged = { id: string; file: File };

export default function AddPage({
  libraries,
  onUploaded,
}: {
  libraries: Library[];
  onUploaded: () => void;
}) {
  const [staged, setStaged] = useState<Staged[]>([]);
  // Derived, not backfilled. `libraries` arrives from a fetch, so on the first
  // render there is nothing to default to — which used to be handled by an
  // effect that set the state once the list turned up, costing a second render
  // and leaving the select momentarily blank. Falling back at render is the
  // same default with neither problem, and an explicit choice still wins.
  const [chosen, setChosen] = useState<string | null>(null);
  const libraryId = chosen ?? libraries[0]?.id ?? "";
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [watching, setWatching] = useState<string[]>([]);
  const [progress, setProgress] = useState<FileProgress[]>([]);
  const [logFor, setLogFor] = useState<FileProgress | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const camera = useRef<HTMLInputElement>(null);

  const hasCamera =
    typeof navigator !== "undefined" &&
    (navigator.maxTouchPoints > 0 || /Android|iPhone|iPad/.test(navigator.userAgent));

  function add(files: FileList | null) {
    if (!files?.length) return;
    setError(null);

    // Copied out of the FileList *now*, before anything else runs.
    //
    // A FileList is a live view of its input element, and the change handler
    // resets `input.value` so that picking the same file twice still fires. If
    // the list is only read inside the state updater — which React runs later —
    // that reset has already emptied it, and the picker silently stages
    // nothing. Drag-and-drop was unaffected because `dataTransfer.files`
    // belongs to the event rather than to an element someone clears.
    const picked = Array.from(files);

    setStaged((current) => {
      // Same name and size twice is the double-drop, not two documents. The
      // archive would dedupe it by content anyway; catching it here means the
      // list shows what will actually happen.
      const seen = new Set(current.map((s) => `${s.file.name}:${s.file.size}`));
      const additions = picked
        .filter((file) => !seen.has(`${file.name}:${file.size}`))
        .map((file) => ({ id: `${file.name}:${file.size}:${file.lastModified}`, file }));
      return [...current, ...additions].slice(0, MAX_STAGED);
    });
  }

  const totalBytes = useMemo(
    () => staged.reduce((sum, item) => sum + item.file.size, 0),
    [staged],
  );

  async function uploadAll() {
    if (!staged.length || !libraryId) return;
    setBusy(true);
    setError(null);
    const ids: string[] = [];
    try {
      for (const item of staged) {
        // Sequential on purpose: a scanner batch of forty files uploaded in
        // parallel is forty OCR jobs competing for the same worker and a
        // progress view that jumps around. One at a time is honest.
        const result = await api.upload(libraryId, item.file);
        ids.push(result.source_file.id);
      }
      setStaged([]);
      setWatching(ids);
      onUploaded();
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? caught.message
          : "That upload did not go through. Nothing was added.",
      );
    } finally {
      setBusy(false);
    }
  }

  const poll = useCallback(async () => {
    try {
      const result = await api.pipelineFiles(watching.length ? watching : undefined);
      setProgress(result.files);
    } catch {
      // The pipeline view is an observation; a failed poll should not take over
      // a page whose upload already succeeded.
    }
  }, [watching]);

  // Pushed, not polled. Every stage transition in the worker announces itself,
  // so this refetches when something actually moved — which is both far less
  // traffic than the old timer and, more importantly, immediate.
  useLiveQuery(["files", "jobs"], poll);

  return (
    <div className="mx-auto max-w-4xl space-y-5">
      <PageHeader icon={UploadCloud} title="Add files">
        Drop anything in — a PDF, a photo of a receipt, a whole scanner batch. You can
        look over what you picked before it goes anywhere, and watch what happens to it
        afterwards.
      </PageHeader>

      {error && (
        <p role="alert" className="rounded-lg border border-red-900 bg-red-950/40 p-3 text-sm text-red-300">
          {error}
        </p>
      )}

      <div
        onDragOver={(event) => {
          event.preventDefault();
          event.stopPropagation();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          // The shell also listens for drops, so that dropping anywhere in the
          // app uploads. Here that is exactly wrong: the whole point of this
          // page is looking at the files *before* they go in, and without this
          // a drop both staged them and uploaded them behind your back.
          event.stopPropagation();
          setDragging(false);
          add(event.dataTransfer.files);
        }}
        className={`rounded-xl border-2 border-dashed p-10 text-center transition-colors ${
          dragging ? "border-accent bg-accent/5" : "border-edge bg-surface/40"
        }`}
      >
        <UploadCloud
          size={30}
          className={`mx-auto ${dragging ? "text-accent" : "text-muted"}`}
        />
        <p className="mt-3 text-sm">
          Drop files here, or{" "}
          <button
            type="button"
            onClick={() => input.current?.click()}
            className="text-accent underline underline-offset-2"
          >
            choose them
          </button>
          {hasCamera && (
            <>
              {" · "}
              <button
                type="button"
                onClick={() => camera.current?.click()}
                className="text-accent underline underline-offset-2"
              >
                take a photo
              </button>
            </>
          )}
        </p>
        <p className="mt-1 text-xs text-muted">
          PDFs, images, HEIC from a phone. Originals are stored exactly as they arrive
          and are never modified.
        </p>

        <input
          ref={input}
          type="file"
          multiple
          className="hidden"
          onChange={(event) => {
            add(event.target.files);
            event.target.value = "";
          }}
        />
        <input
          ref={camera}
          type="file"
          accept="image/*"
          capture="environment"
          className="hidden"
          onChange={(event) => {
            add(event.target.files);
            event.target.value = "";
          }}
        />
      </div>

      {staged.length > 0 && (
        <section className="rounded-xl border border-edge bg-surface">
          <header className="flex flex-wrap items-center gap-3 border-b border-edge px-4 py-3">
            <h2 className="text-sm font-medium">
              {staged.length} file{staged.length === 1 ? "" : "s"} ready
            </h2>
            <span className="text-xs text-muted">{formatBytes(totalBytes)}</span>
            <span className="flex-1" />

            {libraries.length > 1 && (
              <>
                <label htmlFor="target-library" className="text-xs text-muted">
                  Into
                </label>
                <select
                  id="target-library"
                  value={libraryId}
                  onChange={(event) => setChosen(event.target.value)}
                  className="rounded border border-field bg-ink px-2 py-1 text-xs"
                >
                  {libraries.map((library) => (
                    <option key={library.id} value={library.id}>
                      {library.name}
                    </option>
                  ))}
                </select>
              </>
            )}

            <button
              type="button"
              onClick={() => setStaged([])}
              disabled={busy}
              className="rounded border border-edge px-2.5 py-1 text-xs text-muted hover:text-neutral-100 disabled:opacity-40"
            >
              Clear
            </button>
            <button
              type="button"
              onClick={() => void uploadAll()}
              disabled={busy || !libraryId}
              className="flex items-center gap-1.5 rounded bg-accent px-3 py-1.5 text-sm font-medium text-ink disabled:opacity-40"
            >
              <Upload size={14} />
              {busy ? "Adding…" : `Add ${staged.length}`}
            </button>
          </header>

          <ul className="max-h-80 divide-y divide-edge/60 overflow-y-auto">
            {staged.map((item) => (
              <li key={item.id} className="flex items-center gap-3 px-4 py-2">
                <FileText size={15} className="shrink-0 text-muted" />
                <span className="min-w-0 flex-1 truncate text-sm">{item.file.name}</span>
                <span className="shrink-0 text-xs text-muted">
                  {formatBytes(item.file.size)}
                </span>
                <button
                  type="button"
                  onClick={() =>
                    setStaged((current) => current.filter((s) => s.id !== item.id))
                  }
                  disabled={busy}
                  aria-label={`Remove ${item.file.name}`}
                  className="rounded p-1 text-muted hover:text-red-300 disabled:opacity-40"
                >
                  <Trash2 size={14} />
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {progress.length > 0 && (
        <>
          <PipelineFlow files={progress} />

          <section className="rounded-xl border border-edge bg-surface">
            <header className="flex items-center gap-2 border-b border-edge px-4 py-3">
              <h2 className="text-sm font-medium">
                {watching.length ? "What you just added" : "Recently added"}
              </h2>
              <span className="flex-1" />
              {watching.length > 0 && (
                <button
                  type="button"
                  onClick={() => setWatching([])}
                  className="flex items-center gap-1 rounded border border-edge px-2 py-1 text-xs text-muted hover:text-neutral-100"
                >
                  <Plus size={12} />
                  Show everything recent
                </button>
              )}
            </header>
            <ul className="divide-y divide-edge/60">
              {progress.map((file) => (
                <FileRow key={file.source_file_id} file={file} onShowLog={setLogFor} />
              ))}
            </ul>
          </section>
        </>
      )}

      {logFor && (
        <LogViewer
          sourceFileId={logFor.source_file_id}
          title={`Log — ${logFor.original_filename ?? "file"}`}
          onClose={() => setLogFor(null)}
        />
      )}
    </div>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
