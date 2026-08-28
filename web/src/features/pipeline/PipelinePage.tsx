import { Activity } from "lucide-react";

import PageHeader from "../../components/PageHeader";
import { useCallback, useEffect, useState } from "react";

import { api, type PipelineStatus } from "../../api";
import PendingReviewPanel from "../../components/PendingReview";

// "Nothing fails silently" is an invariant, so this screen is the place a failed
// document is guaranteed to surface — and the place it can be retried.
const POLL_MS = 5000;

export default function PipelinePage() {
  const [status, setStatus] = useState<PipelineStatus | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(() => api.pipeline().then(setStatus).catch(() => {}), []);

  useEffect(() => {
    void load();
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  async function retry(jobId: string) {
    setBusy(jobId);
    try {
      await api.retryJob(jobId);
      await load();
    } finally {
      setBusy(null);
    }
  }

  if (!status) {
    return <div className="mx-auto max-w-4xl py-8 text-center text-muted">Loading…</div>;
  }

  const quiet =
    status.counts.length === 0 && status.attention.length === 0 && status.in_flight.length === 0;

  return (
    <div className="mx-auto max-w-4xl">
      <PageHeader icon={Activity} title="Pipeline">
        Every job in the archive, and every one that needs a human.
      </PageHeader>

      {/* Above the job list on purpose. "Nothing in flight" is true and was
          also, until now, the only thing this screen said while documents sat
          permanently unclassified. */}
      <div className="mb-6">
        <PendingReviewPanel />
      </div>

      {quiet ? (
        <p className="rounded-lg border border-edge p-8 text-center text-sm text-muted">
          Nothing in flight.
        </p>
      ) : (
        <>
          <Section title="Needs attention" count={status.attention.length}>
            {status.attention.length === 0 ? (
              <Quiet>Nothing failed.</Quiet>
            ) : (
              <ul className="divide-y divide-edge rounded-lg border border-edge">
                {status.attention.map((job) => (
                  <li key={job.id} className="flex items-start justify-between gap-4 p-4">
                    <div className="min-w-0">
                      <p className="text-sm">
                        <span className="font-medium">{job.stage}</span>
                        <StateChip state={job.state} />
                        <span className="ml-2 text-xs text-muted">
                          {job.attempts} {job.attempts === 1 ? "attempt" : "attempts"}
                        </span>
                      </p>
                      {job.last_error && (
                        <p className="mt-1 font-mono text-xs break-words text-red-400/90">
                          {job.last_error}
                        </p>
                      )}
                    </div>
                    <button
                      onClick={() => retry(job.id)}
                      disabled={busy === job.id}
                      className="shrink-0 rounded-md border border-edge px-3 py-1.5 text-sm hover:border-accent/60 disabled:opacity-40"
                    >
                      {busy === job.id ? "Retrying…" : "Retry"}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Section>

          <Section title="In flight" count={status.in_flight.length}>
            {status.in_flight.length === 0 ? (
              <Quiet>Idle.</Quiet>
            ) : (
              <ul className="divide-y divide-edge rounded-lg border border-edge">
                {status.in_flight.map((job) => (
                  <li key={job.id} className="p-4 text-sm">
                    <span className="font-medium">{job.stage}</span>
                    <StateChip state={job.state} />
                  </li>
                ))}
              </ul>
            )}
          </Section>

          <Section title="All jobs">
            <table className="w-full text-sm">
              <thead className="text-xs text-muted uppercase">
                <tr>
                  <th className="py-2 text-left font-medium">Stage</th>
                  <th className="py-2 text-left font-medium">State</th>
                  <th className="py-2 text-right font-medium">Count</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-edge">
                {status.counts.map((row) => (
                  <tr key={`${row.stage}-${row.state}`}>
                    <td className="py-2">{row.stage}</td>
                    <td className="py-2 text-muted">{row.state.replace(/_/g, " ")}</td>
                    <td className="py-2 text-right font-mono">{row.count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>
        </>
      )}
    </div>
  );
}

function Section({
  title,
  count,
  children,
}: {
  title: string;
  count?: number;
  children: React.ReactNode;
}) {
  return (
    <section className="mb-8">
      <h2 className="mb-2 text-xs font-medium tracking-wide text-muted uppercase">
        {title}
        {count !== undefined && count > 0 && <span className="ml-2 text-accent">{count}</span>}
      </h2>
      {children}
    </section>
  );
}

function Quiet({ children }: { children: React.ReactNode }) {
  return <p className="rounded-lg border border-edge p-4 text-sm text-muted">{children}</p>;
}

function StateChip({ state }: { state: string }) {
  const tone =
    state === "dead_letter"
      ? "border-red-500/40 text-red-400"
      : state === "failed"
        ? "border-amber-500/40 text-amber-400"
        : "border-edge text-muted";
  return (
    <span className={`ml-2 rounded-full border px-2 py-0.5 text-xs ${tone}`}>
      {state.replace(/_/g, " ")}
    </span>
  );
}
