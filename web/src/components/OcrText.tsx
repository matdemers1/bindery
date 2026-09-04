import { useCallback, useEffect, useState } from "react";

import { ApiError, api, type OcrText } from "../api";
import { Alert, Button } from "@d3cloud/ui";

/**
 * What OCR actually read, verbatim.
 *
 * Search failing is ambiguous: the archive might not contain the thing, or OCR
 * might have read your surname as "Dernevs". Only one of those is worth
 * spending an afternoon on, and until you can see the extracted text there is
 * no way to tell them apart.
 *
 * It matters most exactly where OCR is least reliable — handwriting, carbon
 * copies, faint fax paper — so the text is shown raw: original line breaks, no
 * cleanup, no summary. Tidying it would destroy the only evidence.
 *
 * A page that yielded nothing is called out rather than shown as a blank gap,
 * because "OCR read nothing here" and "this page is blank" look identical and
 * mean very different things.
 */
export default function OcrTextPanel({
  sourceFileId,
  onRescan,
}: {
  sourceFileId: string;
  onRescan?: () => void;
}) {
  const [text, setText] = useState<OcrText | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  const load = useCallback(async () => {
    try {
      setText(await api.ocrText(sourceFileId));
      setError(null);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    }
  }, [sourceFileId]);

  useEffect(() => {
    // An async data load: the state is genuinely unavailable on the first
    // render, so the extra pass is the point rather than a mistake.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (open && !text) void load();
  }, [open, text, load]);

  return (
    <section className="rounded-lg border border-edge bg-surface">
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left"
      >
        <span className="text-sm font-medium">What the OCR read</span>
        <span className="text-xs text-muted">
          {text ? `${text.characters.toLocaleString()} characters` : open ? "" : "show"}
        </span>
      </button>

      {open && (
        <div className="border-t border-edge px-4 py-3">
          {error && (
            <p role="alert" className="text-sm text-danger">
              {error}
            </p>
          )}
          {!text && !error && <p className="text-sm text-muted">Loading…</p>}

          {text && (
            <>
              <p className="mb-3 text-xs text-muted">
                Exactly what was extracted, unedited. If a word here is wrong, that is
                why searching for it does not find this document.
              </p>

              {text.characters === 0 && (
                <Alert
                  tone="warning"
                  className="mb-3"
                  title="Nothing was extracted from this file at all."
                  actions={
                    onRescan ? (
                      <Button variant="primary" size="sm" onClick={onRescan}>
                        Rescan this file
                      </Button>
                    ) : undefined
                  }
                >
                  It is stored and safe, but no part of it is searchable. This
                  usually means the page was drawn as vector content, which OCR
                  skips by default — a rescan forces it.
                </Alert>
              )}

              <ol className="space-y-3">
                {text.pages.map((page) => (
                  <li key={page.page_number}>
                    <p className="text-xs uppercase tracking-wide text-muted">
                      Page {page.page_number}
                      {page.characters === 0 && " · nothing read"}
                    </p>
                    {page.characters === 0 ? (
                      <p className="mt-1 text-sm text-muted">
                        OCR produced no text for this page.
                      </p>
                    ) : (
                      <pre className="mt-1 max-h-80 overflow-auto whitespace-pre-wrap break-words rounded bg-ink p-3 font-mono text-xs leading-relaxed">
                        {page.text}
                      </pre>
                    )}
                  </li>
                ))}
              </ol>
            </>
          )}
        </div>
      )}
    </section>
  );
}
