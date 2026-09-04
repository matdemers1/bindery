import { useState } from "react";
import { FileWarning } from "lucide-react";

import { fileUrl } from "../api";

/**
 * A page thumbnail that fails legibly.
 *
 * A raw `<img>` whose source 404s renders as the browser's broken-image glyph
 * next to its alt text — which in a grid of pages looks like the application is
 * broken rather than like one render being missing. And a missing render is not
 * hypothetical: it is what you get when OCR skipped a page, when a file arrived
 * in a format the renderer could not open, or while the pipeline is still
 * working through a fresh upload.
 *
 * So the fallback says which page it is and that the *page* is unavailable —
 * the document itself is stored and fine, and the placeholder should not imply
 * otherwise.
 */
export default function PageThumb({
  sourceFileId,
  page,
  className = "",
  alt,
}: {
  sourceFileId: string;
  page: number;
  className?: string;
  alt?: string;
}) {
  const [failed, setFailed] = useState(false);

  if (failed) {
    return (
      <div
        role="img"
        aria-label={alt ?? `Page ${page}, no preview available`}
        title="No preview was generated for this page. The document itself is stored and intact."
        className={`flex flex-col items-center justify-center gap-1.5 rounded border border-dashed border-edge bg-ink/50 text-muted ${className}`}
      >
        <FileWarning size={18} aria-hidden />
        <span className="text-11">p. {page}</span>
      </div>
    );
  }

  return (
    <img
      src={fileUrl.thumb(sourceFileId, page)}
      alt={alt ?? `Page ${page}`}
      loading="lazy"
      onError={() => setFailed(true)}
      className={className}
    />
  );
}
