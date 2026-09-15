import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router";
import {
  FileLock2,
  Film,
  Images,
  Lock,
  LockOpen,
  Search,
  ShieldCheck,
  Undo2,
} from "lucide-react";

import { api, fileUrl, type VaultItem, type VaultSearchResults, type VaultState } from "../../api";
import SetupForm from "./SetupForm";
import VaultGrid from "./VaultGrid";
import VaultVideos from "./VaultVideos";
import UnlockForm from "./UnlockForm";
import { Alert, Button, Input, Link as TextLink, PageHeader, TabPanel, Tabs } from "@d3cloud/ui";

/**
 * The private vault.
 *
 * Everything on this screen is behind a second secret, and the screen itself is
 * careful about what it says while shut: a locked vault reports that it exists
 * and nothing else, because a count of what is inside is already a statement
 * about the contents.
 *
 * It stays open for fifteen minutes of not being used and then forgets the key.
 * That is a session in memory, not a cookie — restarting the server locks it.
 */
export default function VaultPage() {
  const [state, setState] = useState<VaultState | null>(null);
  const [items, setItems] = useState<VaultItem[]>([]);
  const [q, setQ] = useState("");
  const [results, setResults] = useState<VaultSearchResults | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  // Null until the person picks one, so the default can follow what is
  // actually in the vault: opening on an empty Documents tab because
  // everything you vaulted was a photograph is the wrong first impression.
  const [tab, setTab] = useState<"documents" | "photos" | "videos" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (next: VaultState) => {
    setState(next);
    setItems(next.unlocked ? await api.vaultItems() : []);
    if (!next.unlocked) setResults(null);
  }, []);

  useEffect(() => {
    void api.vault().then(load);
  }, [load]);

  async function lock() {
    await load(await api.vaultLock());
    setQ("");
  }

  /**
   * Taking something back out.
   *
   * The plaintext is written and verified against the hash recorded when it
   * went in before the document becomes visible again, so a failure here leaves
   * it in the vault rather than half out.
   */
  async function moveOut(documentId: string) {
    setBusy(documentId);
    setError(null);
    try {
      await api.vaultMoveOut(documentId);
      setItems(await api.vaultItems());
      setResults(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(null);
    }
  }

  async function runSearch(event: React.FormEvent) {
    event.preventDefault();
    if (!q.trim()) {
      setResults(null);
      return;
    }
    setBusy("search");
    try {
      setResults(await api.vaultSearch(q));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(null);
    }
  }

  if (!state) {
    return <p className="text-sm text-muted">Loading…</p>;
  }

  const videos = items.filter((item) => item.is_video);
  const photos = items.filter((item) => item.is_image && !item.is_video);
  const documents = items.filter((item) => !item.is_image && !item.is_video);
  // Videos is offered only when there are any: a third tab that is always
  // empty is furniture. Photos and Documents are always there, because the
  // action to fill them is one click away on every document.
  type Tab = "documents" | "photos" | "videos";
  const tabs: Tab[] = videos.length ? ["documents", "photos", "videos"] : ["documents", "photos"];
  const active: Tab =
    tab && tabs.includes(tab)
      ? tab
      : documents.length === 0 && photos.length === 0 && videos.length > 0
        ? "videos"
        : documents.length === 0 && photos.length > 0
          ? "photos"
          : "documents";

  return (
    <div className="mx-auto max-w-4xl space-y-4">
      <PageHeader icon={<ShieldCheck size={20} strokeWidth={1.8} />} title="Vault"
        description={
          <>
          A second lock, with its own passphrase. What is in here is encrypted on
          disk, kept out of search, Ask, the archive and every count, and can only
          be read while this screen is open.
          </>
        }
      />

      {!state.exists && <SetupForm onCreated={load} />}

      {state.exists && !state.unlocked && (
        <div className="max-w-md space-y-3 rounded-xl border border-edge bg-surface p-5">
          <p className="flex items-center gap-2 text-sm font-medium">
            <Lock size={15} /> The vault is locked.
          </p>
          <UnlockForm state={state} onUnlocked={load} />
        </div>
      )}

      {state.exists && state.unlocked && (
        <>
          <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-accent/40 bg-accent/5 px-3 py-2">
            <span className="flex items-center gap-2 text-sm text-accent">
              <LockOpen size={15} />
              Open. It locks itself after fifteen minutes.
            </span>
            <Button onClick={() => void lock()}
              icon={<Lock size={14} />}
            >
              Lock now
            </Button>
          </div>

          {/* The vault screen is the one place a vaulted document may be
              spoken about at all (ADR-012), and it stayed silent: the scan
              finished and the counts appeared with nothing announced. Counts
              and states only, and only while the vault is open. */}
          <p role="status" aria-live="polite" className="sr-only">
            {busy === "search"
              ? "Scanning the vault…"
              : results
                ? `${results.total} match${results.total === 1 ? "" : "es"} across ${results.pages_scanned} page${results.pages_scanned === 1 ? "" : "s"}${results.slow ? " — this scan is getting slow." : ""}`
                : ""}
          </p>

          <form onSubmit={runSearch}>
            <label htmlFor="vault-search" className="sr-only">
              Search the vault
            </label>
            <Input
              id="vault-search"
              leading={<Search size={15} />}
              invalid={Boolean(error)}
              aria-describedby={error ? "vault-search-error" : undefined}
              value={q}
              onChange={(event) => setQ(event.target.value)}
              placeholder="Search inside the vault…"
            />
          </form>

          {results && (
            <div className="space-y-2">
              <p className="text-xs text-muted">
                {results.total} match{results.total === 1 ? "" : "es"} across{" "}
                {results.pages_scanned} page{results.pages_scanned === 1 ? "" : "s"} ·{" "}
                {results.elapsed_ms}ms
                {results.slow && (
                  <span className="ml-1 text-warning">
                    — the vault is large enough that this scan is getting slow.
                  </span>
                )}
              </p>
              {results.hits.map((hit) => (
                <div
                  key={`${hit.document_id}-${hit.page_number}`}
                  className="rounded-lg border border-edge bg-surface p-3"
                >
                  <p className="text-sm font-medium">
                    {hit.title ?? "Untitled"}{" "}
                    <span className="font-normal text-muted">· page {hit.page_number}</span>
                  </p>
                  <p className="mt-1 text-sm text-muted">{hit.snippet}</p>
                </div>
              ))}
            </div>
          )}

          {error && (
            <Alert tone="danger" dynamic id="vault-search-error">
              {error}
            </Alert>
          )}

          {items.length === 0 ? (
            <p className="rounded-xl border border-edge bg-surface p-8 text-center text-sm text-muted">
              Nothing is in the vault yet. Open a document or a photo and choose
              “Move to vault”.
            </p>
          ) : (
            <>
              {/* Two kinds of thing, two shapes. A photograph in a table row
                  is as unhelpful here as it is on the Photos screen.

                  A 2px underline and a colour change were the only statement
                  of which list is below — three identically-shaped buttons to
                  a screen reader, and to anyone who does not see the accent.
                  Same shape as the Unify strip on Organise. */}
              {/* Was a hand-rolled tablist: `role="tab"` on three buttons with no
                  arrow keys and every one of them in the tab order, which is a
                  tablist a keyboard cannot drive. `Tabs` owns that contract.
                  Automatic activation is right here — the three lists are
                  already in memory, so choosing costs nothing. */}
              <Tabs
                aria-label="What is in the vault"
                value={active}
                onValueChange={(next) => setTab(next as Tab)}
                items={tabs.map((which) => ({
                  value: which,
                  label: which === "photos" ? "Photos" : which === "videos" ? "Videos" : "Documents",
                  count:
                    which === "photos" ? photos.length
                    : which === "videos" ? videos.length
                    : documents.length,
                  icon:
                    which === "photos" ? <Images size={14} />
                    : which === "videos" ? <Film size={14} />
                    : <FileLock2 size={14} />,
                }))}
              >
                <TabPanel value="photos">
                  <VaultGrid items={photos} busy={busy} onTakeOut={(id) => void moveOut(id)} />
                </TabPanel>
                <TabPanel value="videos">
                  <VaultVideos items={videos} busy={busy} onTakeOut={(id) => void moveOut(id)} />
                </TabPanel>
                <TabPanel value="documents">
                  {documents.length === 0 ? (
                  <p className="rounded-xl border border-edge bg-surface p-8 text-center text-sm text-muted">
                    Nothing here is a document — look under Photos{videos.length ? " or Videos" : ""}.
                  </p>
                ) : (
                  <ul className="space-y-2">
                    {documents.map((item) => (
                      <li
                        key={item.document_id}
                        className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-edge bg-surface p-3"
                      >
                        <div className="min-w-0">
                          <p className="flex items-center gap-2 truncate text-sm font-medium">
                            <FileLock2 size={14} className="shrink-0 text-muted" />
                            {item.title ?? item.original_filename ?? "Untitled"}
                          </p>
                          <p className="mt-0.5 text-xs text-muted">
                            {item.page_count} page{item.page_count === 1 ? "" : "s"} ·{" "}
                            {(item.byte_size / 1024 / 1024).toFixed(1)} MB
                            {item.vaulted_at && ` · vaulted ${item.vaulted_at.slice(0, 10)}`}
                          </p>
                        </div>
                        <div className="flex items-center gap-2">
                          <a
                            href={fileUrl.vaultOriginal(item.document_id)}
                            target="_blank"
                            rel="noreferrer"
                            className="rounded-md border border-edge px-3 py-1.5 text-xs hover:border-accent/60"
                          >
                            Open original
                          </a>
                          <Button size="sm" onClick={() => void moveOut(item.document_id)} disabled={busy === item.document_id} title="Decrypt it back into the archive, where it becomes searchable again"
                            icon={<Undo2 size={13} />}
                          >
                            {busy === item.document_id ? "Restoring…" : "Take out"}
                          </Button>
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
                </TabPanel>
              </Tabs>
            </>
          )}

          <p className="text-xs text-muted">
            Vaulted documents keep their place in{" "}
            <TextLink asChild variant="inline"><Link to="/trust">
              backups and offsite copies
            </Link></TextLink>{" "}
            as ciphertext. A restore brings them back still encrypted, and still
            needing this passphrase.
          </p>
        </>
      )}
    </div>
  );
}
