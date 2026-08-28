import {
  AlertTriangle,
  CheckCircle2,
  Copy,
  FileText,
  Layers,
  Loader,
  ScanLine,
  Split,
} from "lucide-react";

import type { FileProgress } from "../../api";

/**
 * Where every file is, drawn as the pipeline it is actually moving through.
 *
 * A progress bar would be a lie here: this is not one operation with a
 * percentage, it is five distinct stages that fail for five distinct reasons,
 * and "stuck at OCR" and "stuck at filing" need completely different responses
 * from you. So the stages are drawn as stages, and a file sits visibly at one.
 *
 * The states a file can end in — already had it, could not process it — are
 * deliberately *not* nodes in the chain. They are outcomes, and putting them in
 * the line would imply every file passes through them.
 */
const STAGES = [
  { key: "received", label: "Received", icon: FileText, blurb: "Stored, byte for byte" },
  { key: "normalizing", label: "Read", icon: ScanLine, blurb: "OCR over every page" },
  { key: "paging", label: "Indexed", icon: Layers, blurb: "Pages made searchable" },
  { key: "segmenting", label: "Split", icon: Split, blurb: "Bundles cut into documents" },
  { key: "processed", label: "Filed", icon: CheckCircle2, blurb: "Titled, dated, tagged" },
] as const;

const INDEX = new Map<string, number>(STAGES.map((stage, index) => [stage.key, index]));

export function positionOf(file: FileProgress): number {
  return INDEX.get(file.state) ?? 0;
}

/** The stage chain, with how many files are sitting at each. */
export default function PipelineFlow({ files }: { files: FileProgress[] }) {
  const counts = STAGES.map(
    (stage) => files.filter((file) => file.state === stage.key).length,
  );
  const duplicates = files.filter((file) => file.state === "duplicate").length;
  const failed = files.filter((file) => file.state === "failed" || file.dead_lettered).length;

  return (
    <div className="rounded-xl border border-edge bg-surface p-5">
      <ol className="flex items-start gap-1 overflow-x-auto">
        {STAGES.map((stage, index) => {
          const Icon = stage.icon;
          const here = counts[index];
          const done = files.length > 0 && files.every((f) => positionOf(f) >= index);
          const active = here > 0;
          return (
            <li key={stage.key} className="flex min-w-0 flex-1 items-start">
              <div className="flex min-w-0 flex-1 flex-col items-center text-center">
                <span
                  className={`relative flex h-10 w-10 items-center justify-center rounded-full border transition-colors ${
                    active
                      ? "border-accent bg-accent/15 text-accent"
                      : done
                        ? "border-emerald-800 bg-emerald-950/40 text-emerald-400"
                        : "border-edge bg-ink text-muted"
                  }`}
                >
                  <Icon size={17} />
                  {here > 0 && (
                    <span className="absolute -right-1.5 -top-1.5 rounded-full bg-accent px-1.5 text-[11px] font-semibold text-ink">
                      {here}
                    </span>
                  )}
                </span>
                <span
                  className={`mt-2 text-xs font-medium ${active ? "text-accent" : ""}`}
                >
                  {stage.label}
                </span>
                <span className="mt-0.5 hidden text-[11px] leading-tight text-muted sm:block">
                  {stage.blurb}
                </span>
              </div>
              {index < STAGES.length - 1 && (
                <span
                  aria-hidden
                  className={`mt-5 h-px min-w-4 flex-1 ${
                    done ? "bg-emerald-800" : "bg-edge"
                  }`}
                />
              )}
            </li>
          );
        })}
      </ol>

      {(duplicates > 0 || failed > 0) && (
        <div className="mt-4 flex flex-wrap gap-2 border-t border-edge pt-3 text-xs">
          {duplicates > 0 && (
            <span className="flex items-center gap-1.5 rounded-full border border-edge px-2.5 py-1 text-muted">
              <Copy size={12} />
              {duplicates} already in the archive
            </span>
          )}
          {failed > 0 && (
            <span className="flex items-center gap-1.5 rounded-full border border-red-900/70 bg-red-950/25 px-2.5 py-1 text-red-300">
              <AlertTriangle size={12} />
              {failed} could not be processed
            </span>
          )}
        </div>
      )}
    </div>
  );
}

/** One file's row: where it is, and what went wrong if anything did. */
export function FileRow({
  file,
  onShowLog,
}: {
  file: FileProgress;
  onShowLog: (file: FileProgress) => void;
}) {
  const index = positionOf(file);
  const broken = file.state === "failed" || file.dead_lettered;
  const duplicate = file.state === "duplicate";
  const finished = file.state === "processed";

  return (
    <li className="px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        {broken ? (
          <AlertTriangle size={15} className="shrink-0 text-red-400" />
        ) : finished ? (
          <CheckCircle2 size={15} className="shrink-0 text-emerald-400" />
        ) : duplicate ? (
          <Copy size={15} className="shrink-0 text-muted" />
        ) : (
          <Loader size={15} className="shrink-0 animate-spin text-accent" />
        )}

        <span className="min-w-0 flex-1 truncate text-sm font-medium">
          {file.original_filename ?? "(no filename)"}
        </span>

        <span className="text-xs text-muted">
          {duplicate
            ? "already had it"
            : broken
              ? `failed at ${file.failed_stage ?? "an early stage"}`
              : finished
                ? `${file.page_count ?? "?"} pages · ${file.document_count} document${
                    file.document_count === 1 ? "" : "s"
                  }`
                : (STAGES[index]?.label ?? file.state)}
        </span>

        <button
          type="button"
          onClick={() => onShowLog(file)}
          className="rounded border border-edge px-2 py-0.5 text-[11px] text-muted hover:text-neutral-100"
        >
          Log
        </button>
      </div>

      {broken && file.last_error && (
        <p className="mt-1.5 rounded border border-red-900/60 bg-red-950/25 px-2.5 py-1.5 font-mono text-[11px] leading-relaxed text-red-300">
          {file.last_error}
        </p>
      )}
    </li>
  );
}
