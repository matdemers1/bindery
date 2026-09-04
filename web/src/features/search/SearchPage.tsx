import { Search as SearchIcon } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";

import { pageLabel } from "../../lib/pages";

import { api, type Library, type SearchResponse, fileUrl } from "../../api";
import { rememberFoundSomething } from "../firstrun/onboarding";
import Snippet from "../../components/Snippet";
import VaultSearchPanel from "../vault/VaultSearchPanel";
import { ErrorState } from "../../components/States";

// Every piece of search state lives in the URL (REQ-028), so a result is a link
// you can send someone, and the back button behaves.
export default function SearchPage({
  libraries,
  userId,
}: {
  libraries: Library[];
  userId?: string;
}) {
  const [params, setParams] = useSearchParams();
  const query = params.get("q") ?? "";
  const libraryFilter = params.getAll("library");
  const formFilter = params.getAll("form");

  const [draft, setDraft] = useState(query);
  const [fetched, setFetched] = useState<SearchResponse | null>(null);
  // What has been searched for, rather than a `loading` flag set at the top of
  // the effect. `setLoading(true)` there is a synchronous state write inside an
  // effect — a guaranteed second render on every keystroke that commits — and
  // it is redundant: "still loading" is exactly "the thing on screen is not
  // what the URL asks for", which the two can be compared to find out.
  const [settled, setSettled] = useState<string | null>(null);
  const signature = [query, libraryFilter.join(","), formFilter.join(",")].join("|");
  const loading = Boolean(query.trim()) && settled !== signature;
  // Derived, not cleared. An empty query has no results by definition, so
  // saying so at render is simpler than an effect that reaches back to null —
  // and it cannot leave last search's results on screen for a frame.
  const response = query.trim() ? fetched : null;
  const [fetchError, setError] = useState<unknown>(null);
  // Scoped to the search it came from, so a failed query does not leave its
  // message above the next one.
  const error = settled === signature ? fetchError : null;

  // Adjusted during render rather than in an effect. The box has to follow the
  // URL — arriving at /search?q=passport, or pressing back — but as an effect
  // that is a second render pass every time the query changes, and React's own
  // guidance is to compare against the previous value here instead.
  const [lastQuery, setLastQuery] = useState(query);
  if (query !== lastQuery) {
    setLastQuery(query);
    setDraft(query);
  }

  useEffect(() => {
    // Nothing to search for. The results are derived away at render rather
    // than cleared here — see `results` below — so this only has to not run.
    if (!query.trim()) return;
    const controller = new AbortController();
    api
      .search(
        { q: query, libraryIds: libraryFilter, knownFormCodes: formFilter },
        controller.signal,
      )
      .then((found) => {
        setFetched(found);
        setSettled(signature);
        // Onboarding finishes here rather than at a dismissed dialog: being
        // told the archive can find things is not the same as having watched
        // it find one of yours (REQ-147).
        if (userId && found.total > 0) rememberFoundSomething(userId);
      })
      .catch((caught) => {
        // Not swallowed: a search that quietly returns nothing is
        // indistinguishable from an archive that does not contain the thing,
        // which is the worst possible failure for this particular box.
        if (controller.signal.aborted) return;
        setError(caught);
        setSettled(signature);
      });
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

  // Submitting the form only rewrites the URL, and the tree quietly swaps a
  // skeleton for a list: nothing said whether the query had run, failed or
  // found nothing, and focus stays in the box (which is right — refining is
  // the next thing you do). One region, mounted from the first paint rather
  // than inserted with its own text, because a live region that appears at the
  // same moment as its content is not reliably read.
  const announcement = !query.trim()
    ? ""
    : error
      ? "That search did not work."
      : loading
        ? "Searching every page in the archive…"
        : response
          ? response.total === 0
            ? `Nothing matched ${response.query}.`
            : `${response.total} ${response.total === 1 ? "file" : "files"} found`
          : "";

  return (
    <div className="mx-auto max-w-5xl">
      {/* Visually hidden: the search box is self-evidently a search box, and a
          heading above it would be furniture. A screen reader still needs to be
          told which page this is. */}
      <h1 className="sr-only">Search</h1>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          update((next) => (draft.trim() ? next.set("q", draft) : next.delete("q")));
        }}
      >
        <div className="relative">
          <SearchIcon
            size={18}
            aria-hidden
            className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 text-muted"
          />
          {/* A placeholder is a hint, not a name: it is gone the moment a
              character is typed, and the <h1> above names the page rather than
              the control. */}
          <label htmlFor="search-query" className="sr-only">
            Search every page in the archive
          </label>
          <input
            id="search-query"
            autoFocus
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="Search every page in the archive…"
            className="w-full rounded-xl border border-field bg-surface py-3 pl-12 pr-4 text-lg outline-none transition-colors focus:border-accent"
          />
        </div>
      </form>

      <p role="status" aria-live="polite" className="sr-only">
        {announcement}
      </p>

      {!query.trim() ? (
        <EmptyPrompt />
      ) : error ? (
        <div className="mt-8">
          <ErrorState error={error} onRetry={() => update((next) => next.set("q", query))} />
        </div>
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

      {/* Its own section, below the ordinary results and off unless asked for.
          See VaultSearchPanel for why they are not merged. */}
      <VaultSearchPanel query={query} />
    </div>
  );
}

function EmptyPrompt() {
  return (
    <div className="mt-16 text-center text-muted">
      <p className="text-lg">Search finds the page, not just the file.</p>
      <p className="mt-2 text-sm">
        Press <Kbd>⌘</Kbd> <Kbd>K</Kbd> from anywhere to jump straight to a page, or{" "}
        <Link to="/" className="underline underline-offset-2">
          ask a question
        </Link>{" "}
        instead.
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
    // Three grey rectangles say nothing a screen reader can use, and the
    // status region above has already said "searching".
    <ul aria-hidden className="mt-8 space-y-3">
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

  const facets = (
    <>
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
    </>
  );
  const anyFacets =
    (response.facets.library ?? []).length > 0 ||
    (response.facets.known_form ?? []).length > 0;

  return (
    <div className="mt-6 flex gap-8">
      <aside className="hidden w-48 shrink-0 md:block">{facets}</aside>

      <div className="min-w-0 flex-1">
        {/* The rail is `display: none` below md, and 320px — a 1280px window
            at 400% zoom, which is where 1.4.10 is measured — is below md. The
            same controls, in a disclosure, so narrowing survives both the
            phone and the zoom. Only one of the two is ever in the tab order:
            whichever is not display:none. */}
        {anyFacets && (
          <details className="mb-3 rounded-lg border border-edge bg-surface px-3 py-2 md:hidden">
            <summary className="cursor-pointer text-sm text-muted">
              Narrow these results
            </summary>
            <div className="mt-3">{facets}</div>
          </details>
        )}
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
                    {/* Page numbers are always disambiguated (REQ-030), and
                        through one helper so the palette, this row and the
                        viewer header cannot drift into three grammars again
                        (D-04). */}
                    {pageLabel({
                      documentPage: result.best_page.document_page_number,
                      documentPageCount: result.page_end - result.page_start + 1,
                      filePage: result.best_page.page_number,
                      filePageCount: result.file_page_count,
                      filename: result.original_filename ?? "the file",
                    })}
                    {result.matching_pages > 1 &&
                      ` · ${result.matching_pages - 1} more matching ${
                        result.matching_pages === 2 ? "page" : "pages"
                      }`}
                  </p>
                  <p className="mt-2 line-clamp-3 text-sm leading-relaxed text-fg">
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
              // Applied or not was a background tint and a text colour and
              // nothing else, so the filter narrowing the results was
              // inaudible and invisible to anyone who does not see the hue.
              aria-pressed={selected.includes(facet.value)}
              onClick={() => onToggle(facet.value)}
              className={`flex w-full items-baseline justify-between gap-2 rounded px-2 py-1 text-left text-sm ${
                selected.includes(facet.value)
                  ? "bg-accent/15 text-accent"
                  : "text-fg hover:bg-surface"
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
