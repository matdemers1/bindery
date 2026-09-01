import { useState } from "react";
import { ImageOff, Undo2, X } from "lucide-react";

import { fileUrl, type VaultItem } from "../../api";

/**
 * Vaulted photographs, as photographs.
 *
 * Once the vault is open its contents are readable, and a list of filenames is
 * the wrong shape for pictures for the same reason it is wrong on the Photos
 * screen: you recognise a photograph instantly and read a filename slowly.
 *
 * **Full originals, not thumbnails.** Sealing a document destroys its derived
 * renders on purpose — they are plaintext copies of the thing being hidden —
 * and regenerating them would put that plaintext back on disk, which is what
 * the vault exists to prevent. Doing it in memory instead would mean carrying
 * Pillow in the api image, which is deliberately kept out of it. So the grid
 * loads the real files, which is fine at the scale a vault is for: a handful
 * of things you would rather nobody saw, not an archive.
 */
export default function VaultGrid({
  items,
  busy,
  onTakeOut,
}: {
  items: VaultItem[];
  busy: string | null;
  onTakeOut: (documentId: string) => void;
}) {
  const [open, setOpen] = useState<VaultItem | null>(null);
  const [broken, setBroken] = useState<Set<string>>(new Set());

  if (items.length === 0) {
    return (
      <p className="rounded-xl border border-edge bg-surface p-8 text-center text-sm text-muted">
        No pictures in the vault. Open a photo and choose “Move to vault”.
      </p>
    );
  }

  return (
    <>
      <ul className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
        {items.map((item) => (
          <li key={item.document_id}>
            <button
              type="button"
              onClick={() => setOpen(item)}
              className="group w-full overflow-hidden rounded-xl border border-edge bg-surface text-left transition-colors hover:border-accent/60"
            >
              {broken.has(item.document_id) ? (
                // A format the browser will not draw — HEIC outside Safari is
                // the usual one. Saying so beats a silent empty square.
                <span className="flex aspect-square w-full flex-col items-center justify-center gap-2 bg-ink text-muted">
                  <ImageOff size={20} aria-hidden />
                  <span className="px-2 text-center text-[11px]">
                    This browser cannot display {item.media_type ?? "this format"}
                  </span>
                </span>
              ) : (
                <img
                  src={fileUrl.vaultOriginal(item.document_id)}
                  alt={item.title ?? item.original_filename ?? "A vaulted picture"}
                  loading="lazy"
                  onError={() =>
                    setBroken((current) => new Set(current).add(item.document_id))
                  }
                  className="aspect-square w-full bg-ink object-contain"
                />
              )}
              <span className="block px-2.5 py-2">
                <span className="block truncate text-xs font-medium">
                  {item.title ?? item.original_filename ?? "Untitled"}
                </span>
                <span className="mt-0.5 block text-[11px] text-muted">
                  {(item.byte_size / 1024 / 1024).toFixed(1)} MB
                  {item.vaulted_at && ` · vaulted ${item.vaulted_at.slice(0, 10)}`}
                </span>
              </span>
            </button>
          </li>
        ))}
      </ul>

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-ink/90 p-6"
          onClick={() => setOpen(null)}
        >
          <div
            className="flex max-h-full w-full max-w-5xl flex-col overflow-hidden rounded-xl border border-edge bg-surface lg:flex-row"
            onClick={(event) => event.stopPropagation()}
          >
            <img
              src={fileUrl.vaultOriginal(open.document_id)}
              alt={open.title ?? "A vaulted picture"}
              className="max-h-[80vh] flex-1 bg-ink object-contain"
            />
            <div className="w-full shrink-0 space-y-3 border-t border-edge p-4 lg:w-72 lg:border-l lg:border-t-0">
              <div className="flex items-start justify-between gap-2">
                <h2 className="text-sm font-medium">
                  {open.title ?? open.original_filename ?? "Untitled"}
                </h2>
                <button type="button" onClick={() => setOpen(null)} aria-label="Close">
                  <X size={15} />
                </button>
              </div>
              <dl className="grid grid-cols-[5rem_1fr] gap-y-1 text-xs">
                <dt className="text-muted">Filename</dt>
                <dd className="truncate">{open.original_filename ?? "—"}</dd>
                <dt className="text-muted">Vaulted</dt>
                <dd>{open.vaulted_at?.slice(0, 10) ?? "—"}</dd>
              </dl>
              <button
                type="button"
                onClick={() => onTakeOut(open.document_id)}
                disabled={busy === open.document_id}
                className="flex items-center gap-1.5 rounded-md border border-edge px-3 py-1.5 text-xs hover:border-accent/60 disabled:opacity-40"
              >
                <Undo2 size={13} />
                {busy === open.document_id ? "Restoring…" : "Take out of the vault"}
              </button>
              <p className="text-[11px] text-muted">
                Nothing here is cached. Closing the vault makes this
                unreadable again until the PIN is entered.
              </p>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
