import { useState } from "react";
import { Download, Film, Undo2, X } from "lucide-react";

import { fileUrl, type VaultItem } from "../../api";
import Modal from "../../components/Modal";
import MetadataPanel, { formatDuration } from "../media/MetadataPanel";

/**
 * Vaulted videos, playable (REQ-192, REQ-195).
 *
 * The `<video>` element asks for byte ranges and the vault answers each one by
 * decrypting only the chunks it covers (ADR-013) — so seeking to the middle of
 * a long clip costs two chunks of memory, not the whole file. There is no
 * poster: derived renders are destroyed on seal, so the card shows what the
 * metadata knows instead.
 *
 * A format the browser cannot play — .avi, .mkv, .wmv — says so and offers the
 * download, rather than a player that never starts.
 */
export default function VaultVideos({
  items,
  busy,
  onTakeOut,
}: {
  items: VaultItem[];
  busy: string | null;
  onTakeOut: (documentId: string) => void;
}) {
  const [open, setOpen] = useState<VaultItem | null>(null);

  if (items.length === 0) {
    return (
      <p className="rounded-xl border border-edge bg-surface p-8 text-center text-sm text-muted">
        No videos in the vault. Open one under Photos → Videos and choose “Move to vault”.
      </p>
    );
  }

  return (
    <>
      <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {items.map((item) => {
          const playable = item.media?.browser_playable ?? true;
          return (
            <li key={item.document_id}>
              <button
                type="button"
                onClick={() => setOpen(item)}
                className="group w-full overflow-hidden rounded-xl border border-edge bg-surface text-left transition-colors hover:border-accent/60"
              >
                <span className="flex aspect-video w-full flex-col items-center justify-center gap-2 bg-ink text-muted">
                  <Film size={24} aria-hidden />
                  {item.media?.duration_seconds != null && (
                    <span className="font-mono text-xs">
                      {formatDuration(item.media.duration_seconds)}
                    </span>
                  )}
                  {!playable && (
                    <span className="px-2 text-center text-[11px]">
                      {item.media_type ?? "this format"} — download to watch
                    </span>
                  )}
                </span>
                <span className="block px-2.5 py-2">
                  <span className="block truncate text-xs font-medium">
                    {item.title ?? item.original_filename ?? "Untitled"}
                  </span>
                  <span className="mt-0.5 block text-[11px] text-muted">
                    {(item.byte_size / 1024 / 1024).toFixed(1)} MB
                    {item.media?.captured_at && ` · ${item.media.captured_at.slice(0, 10)}`}
                  </span>
                </span>
              </button>
            </li>
          );
        })}
      </ul>

      {open && (
        <Modal
          label={open.title ?? open.original_filename ?? "Untitled"}
          onClose={() => setOpen(null)}
          backdropClassName="fixed inset-0 z-50 flex items-center justify-center bg-ink/90 p-6"
          className="flex max-h-full w-full max-w-5xl flex-col overflow-hidden rounded-xl border border-edge bg-surface lg:flex-row"
        >
          {(open.media?.browser_playable ?? true) ? (
            <video
              controls
              preload="metadata"
              src={fileUrl.vaultOriginal(open.document_id)}
              className="max-h-[80vh] flex-1 bg-ink"
            />
          ) : (
            <div className="flex flex-1 flex-col items-center justify-center gap-3 bg-ink p-8 text-center text-sm text-muted">
              <Film size={28} aria-hidden />
              <p>Browsers cannot play {open.media_type ?? "this format"} directly.</p>
              <a
                href={fileUrl.vaultOriginal(open.document_id)}
                className="flex items-center gap-1.5 rounded-md border border-edge px-3 py-1.5 text-xs hover:border-accent/60"
              >
                <Download size={13} /> Download to watch
              </a>
            </div>
          )}
          <div className="w-full shrink-0 space-y-3 border-t border-edge p-4 lg:w-72 lg:border-l lg:border-t-0">
            <div className="flex items-start justify-between gap-2">
              <h2 className="text-sm font-medium">
                {open.title ?? open.original_filename ?? "Untitled"}
              </h2>
              <button type="button" onClick={() => setOpen(null)} aria-label="Close">
                <X size={15} />
              </button>
            </div>
            {open.media && <MetadataPanel media={open.media} />}
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
              Streamed by decrypting only the part you are watching. Nothing is
              cached; locking the vault stops playback.
            </p>
          </div>
        </Modal>
      )}
    </>
  );
}
