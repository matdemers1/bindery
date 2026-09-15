import { useCallback, useState } from "react";
import { AlertTriangle } from "lucide-react";

import { type VersionReport, versionApi } from "../api";
import { useLiveQuery } from "../live/LiveProvider";

/**
 * What is running, in the corner where nobody looks — until they need it.
 *
 * Two things it says that a plain version string cannot. **The worker may be a
 * different build**: it is a separate image, pulled separately, and when it lags
 * nothing appears wrong — the queue drains, the screens render, and a stage
 * quietly behaves like last week. And **the schema may be behind the code**,
 * because migrations are applied explicitly and never on boot (REQ-114), which
 * is correct and which makes "0019 expected, 0018 applied" a real state whose
 * symptom is an obscure column error somewhere else entirely.
 *
 * Both are warnings rather than errors: the system is usually still working,
 * and an alarm for something that is usually fine is an alarm people learn to
 * ignore.
 */
export default function VersionBadge({ collapsed }: { collapsed: boolean }) {
  const [report, setReport] = useState<VersionReport | null>(null);
  const [open, setOpen] = useState(false);

  const load = useCallback(async () => {
    try {
      setReport(await versionApi.report());
    } catch {
      // A version badge that breaks the shell would be an absurd trade.
      setReport(null);
    }
  }, []);
  // Slow: this changes on deploy, which is not an event the app can be told
  // about — the process that would have told you is the one being replaced.
  useLiveQuery([], load, { fallbackMs: 300_000 });

  if (!report) return null;

  const api = report.services.api;
  const schemaDrift = report.schema.in_sync === false;
  const worrying = report.mismatch || schemaDrift;

  if (collapsed) {
    return worrying ? (
      <div className="flex justify-center py-1" title="Versions disagree">
        <AlertTriangle size={14} className="text-warning" role="img" aria-label="Versions disagree" />
      </div>
    ) : null;
  }

  return (
    <div className="px-1 pb-1 text-11 text-muted">
      <button
        type="button"
        onClick={() => setOpen((was) => !was)}
        className="flex w-full items-center gap-1.5 hover:text-fg"
      >
        {worrying && <AlertTriangle size={11} className="text-warning" />}
        <span className="font-mono">{api.short}</span>
        <span className="flex-1 text-left">
          {report.mismatch
            ? "services disagree"
            : schemaDrift
              ? "migration pending"
              : ""}
        </span>
      </button>

      {open && (
        <dl className="mt-1.5 space-y-1 rounded border border-edge bg-ink p-2">
          {Object.entries(report.services).map(([name, build]) => (
            <div key={name} className="flex gap-2">
              <dt className="w-14 shrink-0">{name}</dt>
              <dd className="font-mono">
                {build.stale ? (
                  <span className="text-warning">not reporting</span>
                ) : (
                  build.short
                )}
              </dd>
            </div>
          ))}
          <div className="flex gap-2 border-t border-edge pt-1">
            <dt className="w-14 shrink-0">schema</dt>
            <dd className="font-mono">
              {report.schema.applied ?? "none"}
              {schemaDrift && (
                <span className="ml-1 text-warning">
                  → {report.schema.expected}
                </span>
              )}
            </dd>
          </div>
          {schemaDrift && (
            <p className="pt-1 text-11 leading-snug">
              The code expects a newer schema. Run{" "}
              <code className="font-mono">alembic upgrade head</code> — it is
              never applied automatically on boot, by design.
            </p>
          )}
        </dl>
      )}
    </div>
  );
}
