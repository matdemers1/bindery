import { useEffect, useState } from "react";
import { Link } from "react-router";

import { api, type WhyPanel as WhyPanelData } from "../../api";

/**
 * Why every AI-written field says what it says (REQ-063).
 *
 * A direct read of stored provenance — nothing here is reconstructed, because a
 * reconstructed justification is not one. Each field shows the page it came
 * from and the exact sentence on that page, and the page number links into the
 * viewer so the claim can be checked against the document in two clicks.
 *
 * Confidence is displayed (REQ-065) and visibly separated from what actually
 * decided the filing, because the model's number did not decide it.
 */
export default function WhyPanel({
  documentId,
  fileId,
  onClose,
}: {
  documentId: string;
  fileId?: string;
  onClose?: () => void;
}) {
  const [data, setData] = useState<WhyPanelData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setData(null);
    setError(null);
    api.why(documentId).then(setData).catch(() => setError("Could not load provenance."));
  }, [documentId]);

  if (error) return <p className="p-4 text-sm text-muted">{error}</p>;
  if (!data) return <p className="p-4 text-sm text-muted">Loading…</p>;

  const { classification, provenance, tags } = data;
  const byField = new Map(provenance.map((row) => [row.field_name, row]));

  return (
    <aside className="rounded-lg border border-edge bg-surface">
      <header className="flex items-baseline justify-between border-b border-edge px-4 py-3">
        <h2 className="text-sm font-medium">Why this says what it says</h2>
        {onClose && (
          <button onClick={onClose} className="text-xs text-muted hover:underline">
            Close
          </button>
        )}
      </header>

      {!classification ? (
        <p className="p-4 text-sm text-muted">
          Set manually — there is no AI provenance for this document.
        </p>
      ) : (
        <div className="divide-y divide-edge">
          {["title", "document_date", "correspondent", "document_type"].map((field) => {
            const row = byField.get(field);
            const confidence = classification.confidence[field];
            return (
              <section key={field} className="px-4 py-3">
                <p className="flex items-baseline justify-between gap-2">
                  <span className="text-xs tracking-wide text-muted uppercase">
                    {field.replace(/_/g, " ")}
                  </span>
                  {confidence !== undefined && <Confidence value={confidence} />}
                </p>
                {row ? (
                  <>
                    <blockquote className="mt-1.5 border-l-2 border-accent/50 pl-3 text-sm text-neutral-300 italic">
                      “{row.snippet}”
                    </blockquote>
                    {row.page_number !== null && (
                      <p className="mt-1 text-xs text-muted">
                        {fileId ? (
                          <Link
                            to={`/file/${fileId}/page/${row.page_number}`}
                            className="underline underline-offset-2"
                          >
                            page {row.page_number} of the file
                          </Link>
                        ) : (
                          <>page {row.page_number} of the file</>
                        )}
                      </p>
                    )}
                  </>
                ) : (
                  <p className="mt-1 text-sm text-muted">
                    No evidence recorded for this field.
                  </p>
                )}
              </section>
            );
          })}

          <section className="px-4 py-3">
            <p className="mb-2 text-xs tracking-wide text-muted uppercase">Tags</p>
            {tags.length === 0 ? (
              <p className="text-sm text-muted">No tags.</p>
            ) : (
              <ul className="flex flex-wrap gap-1.5">
                {tags.map((tag) => (
                  <li key={tag.id}>
                    <SourceChip source={tag.source}>{tag.name}</SourceChip>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="px-4 py-3">
            <p className="mb-2 text-xs tracking-wide text-muted uppercase">
              What decided the filing
            </p>
            <p className="mb-2 text-sm">
              {classification.gate_decision === "filed"
                ? "Filed automatically."
                : "Held for your review."}
            </p>
            <ul className="space-y-1 text-sm text-neutral-300">
              {classification.gate_reasons.map((reason) => (
                <li key={reason} className="flex gap-2">
                  <span className="text-accent">·</span>
                  {reason}
                </li>
              ))}
            </ul>
            <p className="mt-3 text-xs text-muted">
              The model's own confidence is shown above for information. It is
              deliberately not part of this decision — it is poorly calibrated.
            </p>
          </section>

          <footer className="px-4 py-3 font-mono text-xs text-muted">
            {classification.model} · prompt {classification.prompt_version}
          </footer>
        </div>
      )}
    </aside>
  );
}

function Confidence({ value }: { value: number }) {
  return (
    <span className="flex items-center gap-1.5 text-xs text-muted" title="Model confidence">
      <span className="block h-1 w-10 overflow-hidden rounded-full bg-edge">
        <span
          className="block h-full rounded-full bg-muted"
          style={{ width: `${Math.round(value * 100)}%` }}
        />
      </span>
      {Math.round(value * 100)}%
    </span>
  );
}

/** REQ-064: an AI value must never look like one a human set. */
export function SourceChip({
  source,
  children,
}: {
  source: "ai" | "rule" | "human";
  children: React.ReactNode;
}) {
  const style = {
    ai: "border-dashed border-sky-500/60 text-sky-300",
    rule: "border-violet-500/60 text-violet-300",
    human: "border-edge text-neutral-200",
  }[source];
  const label = { ai: "set by the classifier", rule: "set by a rule you wrote", human: "set by you" }[
    source
  ];
  return (
    <span title={label} className={`rounded-full border px-2 py-0.5 text-xs ${style}`}>
      {children}
    </span>
  );
}
