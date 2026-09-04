import { useEffect, useState } from "react";
import { Link } from "react-router";

import { ApiError, api, type WhyPanel as WhyPanelData } from "../../api";
import OcrTextPanel from "../../components/OcrText";
import SourceBadge from "../edit/SourceBadge";
import { Alert, Button } from "@d3cloud/ui";

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
  const [rescan, setRescan] = useState<string | null>(null);

  // Adjusted during render rather than cleared in an effect. Opening this on a
  // second document used to show the *first* one's provenance for a frame —
  // which, on a panel whose entire job is saying where a value came from, is
  // the worst possible thing to be briefly wrong about.
  const [shownFor, setShownFor] = useState(documentId);
  if (documentId !== shownFor) {
    setShownFor(documentId);
    setData(null);
    setError(null);
    setRescan(null);
  }

  useEffect(() => {
    api.why(documentId).then(setData).catch(() => setError("Could not load provenance."));
  }, [documentId]);

  async function requestRescan(sourceFileId: string) {
    setRescan("Queueing…");
    try {
      setRescan((await api.rescan(sourceFileId)).detail);
    } catch (caught) {
      setRescan(caught instanceof ApiError ? caught.message : "Could not start a rescan.");
    }
  }

  if (error) return <p className="p-4 text-sm text-muted">{error}</p>;
  if (!data) return <p className="p-4 text-sm text-muted">Loading…</p>;

  const { classification, provenance, tags } = data;
  const byField = new Map(provenance.map((row) => [row.field_name, row]));
  // `provenance` explains AI values and can only ever explain AI values — it
  // hangs off a classification. This is the half that says "you set this",
  // which is the question people actually bring to this panel after they have
  // corrected something and want to know whether it stuck.
  const bySource = new Map(data.field_sources.map((row) => [row.field_name, row]));

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

      {/*
        Above the provenance on purpose. When nothing was extracted, every
        field below is the model's guess over an empty page — reading them
        first and discovering that afterwards is the wrong order.
      */}
      {data.extraction.characters === 0 && (
        <Alert
          tone="warning"
          flush
          title="No text was read from this document."
          actions={
            <Button
              variant="primary"
              loading={rescan !== null}
              onClick={() => void requestRescan(data.source_file_id)}
            >
              {rescan ? "Rescanning…" : "Rescan with forced OCR"}
            </Button>
          }
        >
          Nothing below is based on anything the machine could actually read, and
          none of this document is searchable. The original is stored and intact —
          reading it again is safe and rebuilds only what was derived.
          {rescan && <p className="mt-2">{rescan}</p>}
        </Alert>
      )}

      {data.field_sources.some((row) => row.source === "human") && (
        <p className="border-b border-edge px-4 py-3 text-sm text-muted">
          Fields marked{" "}
          <span className="text-accent">you set this</span> are yours. AI review
          will keep improving the rest and will leave those alone.
        </p>
      )}

      {!classification ? (
        <div className="divide-y divide-edge">
          <p className="px-4 py-3 text-sm text-muted">
            No AI has looked at this document, so there is no model reasoning to
            show — only who set what.
          </p>
          {["title", "document_date", "correspondent_id", "document_type_id"].map(
            (field) => {
              const set = bySource.get(field);
              if (!set) return null;
              return (
                <section key={field} className="px-4 py-3">
                  <p className="flex items-baseline justify-between gap-2">
                    <span className="text-xs tracking-wide text-muted uppercase">
                      {field.replace(/_id$/, "").replace(/_/g, " ")}
                    </span>
                    <SourceBadge source={set.source} when={set.set_at} />
                  </p>
                </section>
              );
            },
          )}
        </div>
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
                  <span className="flex items-center gap-2">
                    <SourceBadge
                      source={
                        bySource.get(field)?.source ??
                        bySource.get(`${field}_id`)?.source
                      }
                      when={
                        bySource.get(field)?.set_at ??
                        bySource.get(`${field}_id`)?.set_at
                      }
                    />
                    {confidence !== undefined && <Confidence value={confidence} />}
                  </span>
                </p>
                {row ? (
                  <>
                    <blockquote className="mt-1.5 border-l-2 border-accent/50 pl-3 text-sm text-fg italic">
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
            <ul className="space-y-1 text-sm text-fg">
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

      {/* Always available, not only when something went wrong: checking what
          OCR read is how you find out whether a search that returned nothing
          was the archive's answer or the scanner's. */}
      <div className="border-t border-edge p-4">
        <OcrTextPanel
          sourceFileId={data.source_file_id}
          onRescan={() => void requestRescan(data.source_file_id)}
        />
      </div>
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
    ai: "border-dashed border-info/60 text-info",
    rule: "border-accent/60 text-accent",
    human: "border-edge text-fg",
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
