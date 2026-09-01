import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";

import { api, type SearchResult } from "../../api";

// ⌘K → type → Enter → the viewer opens on the page. That path is the product.
// Everything here is in service of keeping it under a second.
const DEBOUNCE_MS = 120;
const MAX_RESULTS = 7;

/**
 * Mounted only while it is open, so every open starts empty.
 *
 * It used to be mounted permanently with an `if (!open) return null` and an
 * effect that cleared the query, the results and the selection each time
 * `open` flipped. Not rendering it at all is the same behaviour with no state
 * to remember to reset — and no frame where the previous search is still on
 * screen underneath the new one.
 */
export default function CommandPalette({ onClose }: { onClose: () => void }) {
  const [query, setQuery] = useState("");
  const [fetched, setFetched] = useState<SearchResult[]>([]);
  // Derived rather than cleared: an empty box has no results by definition.
  const results = query.trim() ? fetched : [];
  const [selected, setSelected] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();

  useEffect(() => {
    // Nothing typed. `results` is derived away at render, so this only has to
    // not fetch.
    if (!query.trim()) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      api
        .search({ q: query, limit: MAX_RESULTS }, controller.signal)
        .then((response) => {
          setFetched(response.results);
          setSelected(0);
        })
        .catch(() => {});
    }, DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [query]);


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
        aria-modal="true"
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
          role="combobox"
          aria-expanded={results.length > 0}
          aria-controls="palette-results"
          aria-activedescendant={
            results[selected] ? `palette-result-${results[selected].document_id}` : undefined
          }
          className="w-full border-b border-edge bg-transparent px-4 py-3.5 text-base outline-none"
        />

        {results.length > 0 ? (
          <ul id="palette-results" role="listbox" className="max-h-80 overflow-y-auto py-1">
            {results.map((result, index) => (
              <li key={result.document_id}>
                <button
                  id={`palette-result-${result.document_id}`}
                  role="option"
                  aria-selected={index === selected}
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
