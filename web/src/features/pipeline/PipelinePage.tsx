import { useCallback, useMemo, useState } from "react";
import { Activity, Check, Loader, RotateCw, ScrollText } from "lucide-react";

import { api, type FileProgress, type Job, type PipelineStatus } from "../../api";
import LogViewer from "../../components/LogViewer";
import PageHeader from "../../components/PageHeader";
import PendingReviewPanel from "../../components/PendingReview";
import { useLiveQuery } from "../../live/LiveProvider";
import PipelineFlow, { FileRow } from "../add/PipelineFlow";

/**
 * The pipeline, as a pipeline.
 *
 * This screen used to be three lists and a table of stage/state counts. The
 * table was the honest shape of the data and the wrong shape for the question:
 * nobody wants to know that `normalize` has two rows in `queued`, they want to
 * know *which document is stuck and why*.
 *
 * So it now shows the same stage chain as the add page — one visual language
 * for one concept — plus the files themselves, and a failure list that names
 * the document rather than just the stage that dropped it.
 *
 * The distinction that earns its place here is **retrying** versus **gave up**.
 * A job that failed and will try again is put back on the queue, so it reads as
 * `queued` in the database and used to be reported as perfectly healthy. Two
 * tax documents failed on a loop underneath a screen that said "Nothing
 * failed."
 */
export default function PipelinePage() {
  const [status, setStatus] = useState<PipelineStatus | null>(null);
  const [files, setFiles] = useState<FileProgress[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [logFor, setLogFor] = useState<FileProgress | null>(null);
  const [showAll, setShowAll] = useState(false);

  const load = useCallback(async () => {
    const [jobs, progress] = await Promise.allSettled([
      api.pipeline(),
      api.pipelineFiles(),
    ]);
    if (jobs.status === "fulfilled") setStatus(jobs.value);
    if (progress.status === "fulfilled") setFiles(progress.value.files);
  }, []);

  useLiveQuery(["jobs", "files", "review"], load);

  // The job knows which file it belongs to but not what it is called, and a
  // failure you cannot name is a failure you cannot go and look at.
  const nameOf = useMemo(() => {
    const names = new Map(files.map((f) => [f.source_file_id, f.original_filename]));
    return (job: Job) =>
      (job.source_file_id && names.get(job.source_file_id)) || null;
  }, [files]);

  async function acknowledge(jobId: string, undo: boolean) {
    setBusy(jobId);
    try {
      await api.acknowledgeJob(jobId, undo);
      await load();
    } finally {
      setBusy(null);
    }
  }

  async function retry(jobId: string) {
    setBusy(jobId);
    try {
      await api.retryJob(jobId);
      await load();
    } finally {
      setBusy(null);
    }
  }

  const attention = status?.attention ?? [];
  const retrying = attention.filter((job) => job.state === "queued");
  const gaveUp = attention.filter((job) => job.state !== "queued");
  const visible = showAll ? files : files.slice(0, 10);

  return (
    <div className="mx-auto max-w-4xl space-y-5">
      <PageHeader icon={Activity} title="Pipeline">
        Where every file is, and anything that needs you.
      </PageHeader>

      <PendingReviewPanel />

      {files.length > 0 && <PipelineFlow files={files} />}

      {gaveUp.length > 0 && (
        <JobList
          tone="bad"
          title="Gave up"
          blurb="These stopped retrying on their own. Nothing was lost — the originals are stored — but they will not move again without you. Acknowledged ones stay listed here and stop counting toward the unhandled total on Trust."
          jobs={gaveUp}
          nameOf={nameOf}
          busy={busy}
          onRetry={retry}
          onAcknowledge={acknowledge}
        />
      )}

      {retrying.length > 0 && (
        <JobList
          tone="warn"
          title="Retrying"
          blurb="These failed and are backing off before another attempt. They still read as queued in the database, which is why this screen used to call them healthy."
          jobs={retrying}
          nameOf={nameOf}
          busy={busy}
          onRetry={retry}
        />
      )}

      {(status?.declined.length ?? 0) > 0 && (
        <JobList
          tone="quiet"
          title="Refused"
          blurb="Not documents, and the pipeline said so on the first look — an image a few pixels across, a form only Acrobat can open. Nothing was lost: the originals are stored, and these are listed rather than alerted on because there is nothing to fix."
          jobs={status?.declined ?? []}
          nameOf={nameOf}
          busy={busy}
          onRetry={retry}
        />
      )}

      {(status?.in_flight.length ?? 0) > 0 && (
        <section className="rounded-xl border border-edge bg-surface">
          <header className="flex items-center gap-2 border-b border-edge px-4 py-3">
            <Loader size={15} className="animate-spin text-accent" />
            <h2 className="text-sm font-medium">Running now</h2>
          </header>
          <ul className="divide-y divide-edge/60">
            {status?.in_flight.map((job) => (
              <li key={job.id} className="flex items-center gap-3 px-4 py-2.5 text-sm">
                <span className="rounded bg-edge px-1.5 text-[11px] text-neutral-300">
                  {job.stage}
                </span>
                <span className="min-w-0 flex-1 truncate">
                  {nameOf(job) ?? "a document"}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="rounded-xl border border-edge bg-surface">
        <header className="flex items-center gap-2 border-b border-edge px-4 py-3">
          <h2 className="text-sm font-medium">Files</h2>
          <span className="text-xs text-muted">{files.length}</span>
          <span className="flex-1" />
          {files.length > 10 && (
            <button
              type="button"
              onClick={() => setShowAll((value) => !value)}
              className="rounded border border-field px-2 py-1 text-xs text-muted hover:text-neutral-100"
            >
              {showAll ? "Show recent" : `Show all ${files.length}`}
            </button>
          )}
        </header>
        {files.length === 0 ? (
          <p className="px-4 py-8 text-center text-sm text-muted">
            Nothing has been added yet.
          </p>
        ) : (
          <ul className="divide-y divide-edge/60">
            {visible.map((file) => (
              <FileRow key={file.source_file_id} file={file} onShowLog={setLogFor} />
            ))}
          </ul>
        )}
      </section>

      {logFor && (
        <LogViewer
          sourceFileId={logFor.source_file_id}
          title={`Log — ${logFor.original_filename ?? "file"}`}
          onClose={() => setLogFor(null)}
        />
      )}

      {!logFor && (
        <details className="rounded-xl border border-edge bg-surface">
          <summary className="flex cursor-pointer items-center gap-2 px-4 py-3 text-sm font-medium">
            <ScrollText size={15} className="text-accent" />
            Everything the pipeline logged
          </summary>
          <div className="border-t border-edge p-3">
            <LogViewer title="Pipeline log" defaultLevel="warning" />
          </div>
        </details>
      )}
    </div>
  );
}

function JobList({
  tone,
  title,
  blurb,
  jobs,
  nameOf,
  busy,
  onRetry,
  onAcknowledge,
}: {
  tone: "bad" | "warn" | "quiet";
  title: string;
  blurb: string;
  jobs: Job[];
  nameOf: (job: Job) => string | null;
  busy: string | null;
  onRetry: (jobId: string) => void;
  onAcknowledge?: (jobId: string, undo: boolean) => void;
}) {
  const bad = tone === "bad";
  const quiet = tone === "quiet";
  return (
    <section
      className={`rounded-xl border ${
        quiet
          ? "border-edge bg-surface"
          : bad
            ? "border-red-900/70 bg-red-950/20"
            : "border-amber-900/70 bg-amber-950/15"
      }`}
    >
      <header className="px-4 pb-2 pt-3">
        <h2
          className={`flex items-center gap-2 text-sm font-medium ${
            quiet ? "" : bad ? "text-red-300" : "text-amber-300"
          }`}
        >
          {title}
          <span className="rounded-full bg-black/30 px-1.5 text-[11px]">{jobs.length}</span>
        </h2>
        <p className="mt-1 max-w-2xl text-xs text-muted">{blurb}</p>
      </header>
      <ul className="divide-y divide-white/5">
        {jobs.map((job) => (
          <li key={job.id} className="flex items-start gap-3 px-4 py-3">
            <div className="min-w-0 flex-1">
              <p className="flex flex-wrap items-baseline gap-2 text-sm">
                <span className="font-medium">{nameOf(job) ?? "(unnamed file)"}</span>
                <span className="rounded bg-black/30 px-1.5 text-[11px] text-neutral-300">
                  {job.stage}
                </span>
                <span className="text-xs text-muted">
                  {quiet ? "refused on the first look" : `${job.attempts} of 5 attempts`}
                  {job.state === "queued" && job.scheduled_for
                    ? ` · next ${new Date(job.scheduled_for).toLocaleTimeString()}`
                    : ""}
                </span>
                {job.acknowledged_at && (
                  <span className="text-xs text-muted">· acknowledged</span>
                )}
              </p>
              {job.last_error && (
                // Verbatim and wrapped rather than truncated: the specific
                // message is the whole diagnosis.
                <p
                  className={`mt-1.5 max-h-24 overflow-y-auto whitespace-pre-wrap break-words rounded bg-black/30 px-2 py-1.5 font-mono text-[11px] leading-relaxed ${
                    quiet ? "text-muted" : "text-red-300/90"
                  }`}
                >
                  {job.last_error}
                </p>
              )}
            </div>
            <div className="flex shrink-0 flex-col gap-1.5">
              <button
                onClick={() => onRetry(job.id)}
                disabled={busy === job.id}
                className="flex items-center gap-1.5 rounded-md border border-field px-2.5 py-1.5 text-xs hover:border-accent/60 disabled:opacity-40"
              >
                <RotateCw size={12} className={busy === job.id ? "animate-spin" : ""} />
                {busy === job.id ? "Retrying…" : "Retry now"}
              </button>
              {/* The weakest action available, and deliberately so: it does not
                  retry, hide or remove anything. It only stops the job counting
                  toward the badge, so a warning that is still lit means there is
                  still something to do. */}
              {onAcknowledge && (
                <button
                  onClick={() => onAcknowledge(job.id, !!job.acknowledged_at)}
                  disabled={busy === job.id}
                  title={
                    job.acknowledged_at
                      ? "Count this as outstanding again"
                      : "Stop counting this toward the badge. It stays here, with its error."
                  }
                  className="flex items-center gap-1.5 rounded-md border border-field px-2.5 py-1.5 text-xs text-muted hover:text-neutral-100 disabled:opacity-40"
                >
                  <Check size={12} />
                  {job.acknowledged_at ? "Undo" : "Acknowledge"}
                </button>
              )}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
