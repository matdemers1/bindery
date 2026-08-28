import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";

import { api, type Library, type SearchResponse, fileUrl } from "../../api";
import Snippet from "../../components/Snippet";

// Every piece of search state lives in the URL (REQ-028), so a result is a link
// you can send someone, and the back button behaves.
export default function SearchPage({ libraries }: { libraries: Library[] }) {
  const [params, setParams] = useSearchParams();
  const query = params.get("q") ?? "";
  const libraryFilter = params.getAll("library");
  const formFilter = params.getAll("form");

  const [draft, setDraft] = useState(query);
  const [response, setResponse] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => setDraft(query), [query]);

  useEffect(() => {
    if (!query.trim()) {
      setResponse(null);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    api
      .search(
        { q: query, libraryIds: libraryFilter, knownFormCodes: formFilter },
        controller.signal,
      )
      .then(setResponse)
      .catch(() => {})
      .finally(() => setLoading(false));
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, libraryFilter.join(","), formFilter.join(",")]);

  const update = useCallback(
    (mutate: (next: URLSearchParams) => void) => {
      const next = new URLSearchParams(params);
      mutate(next);
      setParams(next, { replace: true });
    },
    [params, setParams],
  );

  function toggle(key: string, value: string) {
    update((next) => {
      const existing = next.getAll(key);
      next.delete(key);
      const wanted = existing.includes(value)
        ? existing.filter((v) => v !== value)
        : [...existing, value];
      wanted.forEach((v) => next.append(key, v));
    });
  }

  const libraryName = (id: string) =>
    libraries.find((library) => library.id === id)?.name ?? "Unknown library";

  return (
    <div className="mx-auto max-w-5xl px-6 py-8">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          update((next) => (draft.trim() ? next.set("q", draft) : next.delete("q")));
        }}
      >
        <input
          autoFocus
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="Search every page in the archive…"
          className="w-full rounded-lg border border-edge bg-surface px-4 py-3 text-lg outline-none focus:border-accent"
        />
      </form>

      {!query.trim() ? (
        <EmptyPrompt />
      ) : loading && !response ? (
        <Skeleton />
      ) : response ? (
        <Results
          response={response}
          libraryName={libraryName}
          libraryFilter={libraryFilter}
          formFilter={formFilter}
          onToggle={toggle}
          onSuggestion={(suggestion) => update((next) => next.set("q", suggestion))}
        />
      ) : null}
    </div>
  );
}

function EmptyPrompt() {
  return (
    <div className="mt-16 text-center text-muted">
      <p className="text-lg">Search finds the page, not just the file.</p>
      <p className="mt-2 text-sm">
        Press <Kbd>⌘</Kbd> <Kbd>K</Kbd> from anywhere to jump straight to a page.
      </p>
    </div>
  );
}

function Kbd({ children }: { children: React.ReactNode }) {
  return (
    <kbd className="rounded border border-edge bg-surface px-1.5 py-0.5 font-mono text-xs">
      {children}
    </kbd>
  );
}

function Skeleton() {
  return (
    <ul className="mt-8 space-y-3">
      {[0, 1, 2].map((n) => (
        <li key={n} className="h-24 animate-pulse rounded-lg border border-edge bg-surface" />
      ))}
    </ul>
  );
}

function Results({
  response,
  libraryName,
  libraryFilter,
  formFilter,
  onToggle,
  onSuggestion,
}: {
  response: SearchResponse;
  libraryName: (id: string) => string;
  libraryFilter: string[];
  formFilter: string[];
  onToggle: (key: string, value: string) => void;
  onSuggestion: (suggestion: string) => void;
}) {
  if (response.total === 0) {
    return (
      <div className="mt-12 text-center">
        <p className="text-muted">No pages matched “{response.query}”.</p>
        {response.suggestions.length > 0 && (
          <p className="mt-3 text-sm text-muted">
            Did you mean{" "}
            {response.suggestions.map((suggestion, index) => (
              <span key={suggestion}>
                {index > 0 && ", "}
                <button
                  onClick={() => onSuggestion(suggestion)}
                  className="text-accent underline underline-offset-2"
                >
                  {suggestion}
                </button>
              </span>
            ))}
            ?
          </p>
        )}
      </div>
    );
  }

  return (
    <div className="mt-6 flex gap-8">
      <aside className="hidden w-48 shrink-0 md:block">
        <FacetGroup
          title="Library"
          facets={response.facets.library ?? []}
          selected={libraryFilter}
          label={libraryName}
          onToggle={(value) => onToggle("library", value)}
        />
        <FacetGroup
          title="Known form"
          facets={response.facets.known_form ?? []}
          selected={formFilter}
          label={(value) =>
            response.facets.known_form?.find((facet) => facet.value === value)?.label ?? value
          }
          onToggle={(value) => onToggle("form", value)}
        />
      </aside>

      <div className="min-w-0 flex-1">
        <p className="mb-3 text-sm text-muted">
          {response.total} {response.total === 1 ? "file" : "files"}
        </p>
        <ul className="space-y-3">
          {response.results.map((result) => (
            <li key={result.document_id}>
              <Link
                to={`/document/${result.document_id}/page/${result.best_page.document_page_number}?q=${encodeURIComponent(response.query)}`}
                className="flex gap-4 rounded-lg border border-edge bg-surface p-4 hover:border-accent/60"
              >
                <img
                  src={fileUrl.thumb(result.source_file_id, result.best_page.page_number)}
                  alt=""
                  loading="lazy"
                  className="h-28 w-20 shrink-0 rounded border border-edge object-cover object-top"
                />
                <div className="min-w-0">
                  <p className="flex items-center gap-2">
                    <span className="truncate font-medium">
                      {result.title ?? result.original_filename ?? "(untitled)"}
                    </span>
                    {result.known_form_code && (
                      <span
                        title={result.known_form_name ?? undefined}
                        className="shrink-0 rounded-full border border-accent/50 px-2 py-0.5 text-xs text-accent"
                      >
                        {result.known_form_code}
                      </span>
                    )}
                  </p>
                  <p className="mt-0.5 text-xs text-muted">
                    {/* Page numbers are always disambiguated (REQ-030). */}
                    Page {result.best_page.document_page_number} of this document
                    {" · page "}
                    {result.best_page.page_number}
                    {result.file_page_count ? ` of ${result.file_page_count}` : ""} in{" "}
                    {result.original_filename ?? "the file"}
                    {result.matching_pages > 1 &&
                      ` · ${result.matching_pages - 1} more matching ${
                        result.matching_pages === 2 ? "page" : "pages"
                      }`}
                  </p>
                  <p className="mt-2 line-clamp-3 text-sm leading-relaxed text-neutral-300">
                    <Snippet html={result.best_page.snippet} />
                  </p>
                </div>
              </Link>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function FacetGroup({
  title,
  facets,
  selected,
  label,
  onToggle,
}: {
  title: string;
  facets: { value: string; count: number }[];
  selected: string[];
  label: (value: string) => string;
  onToggle: (value: string) => void;
}) {
  if (facets.length === 0) return null;
  return (
    <div className="mb-6">
      <h2 className="mb-2 text-xs font-medium tracking-wide text-muted uppercase">{title}</h2>
      <ul className="space-y-1">
        {facets.map((facet) => (
          <li key={facet.value}>
            <button
              onClick={() => onToggle(facet.value)}
              className={`flex w-full items-baseline justify-between gap-2 rounded px-2 py-1 text-left text-sm ${
                selected.includes(facet.value)
                  ? "bg-accent/15 text-accent"
                  : "text-neutral-300 hover:bg-surface"
              }`}
            >
              <span className="truncate">{label(facet.value)}</span>
              <span className="shrink-0 text-xs text-muted">{facet.count}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
