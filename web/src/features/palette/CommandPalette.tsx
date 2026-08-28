import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";

import { api, type SearchResult } from "../../api";

// ⌘K → type → Enter → the viewer opens on the page. That path is the product.
// Everything here is in service of keeping it under a second.
const DEBOUNCE_MS = 120;
const MAX_RESULTS = 7;

export default function CommandPalette({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [selected, setSelected] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();

  useEffect(() => {
    if (open) {
      setQuery("");
      setResults([]);
      setSelected(0);
    }
  }, [open]);

  useEffect(() => {
    if (!open || !query.trim()) {
      setResults([]);
      return;
    }
    const controller = new AbortController();
    const timer = setTimeout(() => {
      api
        .search({ q: query, limit: MAX_RESULTS }, controller.signal)
        .then((response) => {
          setResults(response.results);
          setSelected(0);
        })
        .catch(() => {});
    }, DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [open, query]);

  if (!open) return null;

  function openResult(result: SearchResult) {
    onClose();
    navigate(
      `/document/${result.document_id}/page/${result.best_page.document_page_number}` +
        `?q=${encodeURIComponent(query)}`,
    );
  }

  function onKeyDown(event: React.KeyboardEvent) {
    if (event.key === "Escape") return onClose();
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setSelected((index) => Math.min(index + 1, results.length - 1));
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setSelected((index) => Math.max(index - 1, 0));
    }
    if (event.key === "Enter") {
      event.preventDefault();
      if (results[selected]) return openResult(results[selected]);
      // No hit yet — fall through to the full results page rather than nothing.
      onClose();
      navigate(`/?q=${encodeURIComponent(query)}`);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/70 p-4 pt-[12vh]"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-label="Command palette"
        onClick={(event) => event.stopPropagation()}
        className="w-full max-w-xl overflow-hidden rounded-xl border border-edge bg-surface shadow-2xl"
      >
        <input
          ref={input}
          // Synchronous autoFocus, not a deferred focus() call: the input mounts
          // fresh each time the palette opens, and anyone who presses ⌘K starts
          // typing immediately — a focus deferred to the next frame loses those
          // first keystrokes to whatever was focused before.
          autoFocus
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Jump to a page…"
          className="w-full border-b border-edge bg-transparent px-4 py-3.5 text-base outline-none"
        />

        {results.length > 0 ? (
          <ul className="max-h-80 overflow-y-auto py-1">
            {results.map((result, index) => (
              <li key={result.document_id}>
                <button
                  onMouseEnter={() => setSelected(index)}
                  onClick={() => openResult(result)}
                  className={`flex w-full items-baseline gap-3 px-4 py-2.5 text-left ${
                    index === selected ? "bg-accent/15" : ""
                  }`}
                >
                  <span className="min-w-0 flex-1 truncate text-sm">
                    {result.title ?? result.original_filename ?? "(untitled)"}
                  </span>
                  {result.known_form_code && (
                    <span className="shrink-0 rounded-full border border-accent/50 px-1.5 text-xs text-accent">
                      {result.known_form_code}
                    </span>
                  )}
                  <span className="shrink-0 font-mono text-xs text-muted">
                    p.{result.best_page.document_page_number}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        ) : query.trim() ? (
          <p className="px-4 py-6 text-center text-sm text-muted">No pages matched.</p>
        ) : (
          <p className="px-4 py-6 text-center text-sm text-muted">
            Type to search every page in the archive.
          </p>
        )}
      </div>
    </div>
  );
}
