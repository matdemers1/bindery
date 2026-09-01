import { useCallback, useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Vault } from "lucide-react";

import { api, type VaultSearchResults, type VaultState } from "../../api";
import UnlockForm from "./UnlockForm";

/**
 * "Look in the vault too", on the search screen.
 *
 * Off by default and asked for explicitly, because the ordinary search is the
 * one that runs over someone's shoulder. Turning it on when the vault is shut
 * prompts for the PIN here rather than sending you somewhere else.
 *
 * Vault hits stay in their own section instead of being mixed into the results
 * above. Merging them would make it ambiguous which results are the private
 * ones — and the whole point is knowing.
 */
export default function VaultSearchPanel({ query }: { query: string }) {
  const [on, setOn] = useState(false);
  const [state, setState] = useState<VaultState | null>(null);
  const [results, setResults] = useState<VaultSearchResults | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const run = useCallback(
    async (signal?: AbortSignal) => {
      if (!query.trim()) {
        setResults(null);
        return;
      }
      setLoading(true);
      setError(null);
      try {
        setResults(await api.vaultSearch(query, signal));
      } catch (caught) {
        if (signal?.aborted) return;
        setError(caught instanceof Error ? caught.message : String(caught));
      } finally {
        setLoading(false);
      }
    },
    [query],
  );

  useEffect(() => {
    if (!on || !state?.unlocked) return;
    const controller = new AbortController();
    // Fetching from the API is exactly the external system this rule carves
    // out; the state genuinely is not available at render time, and the
    // request is aborted on cleanup so a stale query cannot land late.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void run(controller.signal);
    return () => controller.abort();
  }, [on, state?.unlocked, run]);

  async function toggle() {
    if (on) {
      setOn(false);
      setResults(null);
      return;
    }
    setState(await api.vault());
    setOn(true);
  }

  if (!query.trim()) return null;

  return (
    <section className="mt-8 rounded-xl border border-edge bg-surface/50">
      <button
        type="button"
        onClick={() => void toggle()}
        className="flex w-full items-center gap-2 px-4 py-3 text-left text-sm text-muted hover:text-neutral-100"
      >
        {on ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
        <Vault size={15} />
        Also search the vault
        {on && state?.unlocked && results && (
          <span className="ml-auto text-xs">
            {results.total} match{results.total === 1 ? "" : "es"}
          </span>
        )}
      </button>

      {on && state && (
        <div className="space-y-3 border-t border-edge px-4 py-4">
          {!state.exists ? (
            <p className="text-sm text-muted">
              There is no vault on this account.
            </p>
          ) : !state.unlocked ? (
            <>
              <p className="text-sm text-muted">
                The vault is locked. Open it to include it in this search.
              </p>
              <div className="max-w-sm">
                <UnlockForm state={state} onUnlocked={setState} compact />
              </div>
            </>
          ) : loading ? (
            <p className="text-sm text-muted">Decrypting and scanning…</p>
          ) : error ? (
            <p className="rounded-lg border border-red-900/60 bg-red-950/20 px-3 py-2 text-sm text-red-300">
              {error}
            </p>
          ) : results && results.total > 0 ? (
            <>
              {results.slow && (
                <p className="text-xs text-amber-400">
                  Scanned {results.pages_scanned} pages in {results.elapsed_ms}ms.
                  The vault searches by decrypting every page, so this gets
                  slower as it grows.
                </p>
              )}
              <ul className="space-y-2">
                {results.hits.map((hit) => (
                  <li
                    key={`${hit.document_id}-${hit.page_number}`}
                    className="rounded-lg border border-edge bg-surface p-3"
                  >
                    <p className="text-sm font-medium">
                      {hit.title ?? "Untitled"}{" "}
                      <span className="font-normal text-muted">
                        · page {hit.page_number}
                      </span>
                    </p>
                    <p className="mt-1 text-sm text-muted">{hit.snippet}</p>
                  </li>
                ))}
              </ul>
              <p className="text-xs text-muted">
                Vaulted pages have no viewer — open the original from the{" "}
                vault screen.
              </p>
            </>
          ) : (
            <p className="text-sm text-muted">Nothing in the vault matches that.</p>
          )}
        </div>
      )}
    </section>
  );
}
