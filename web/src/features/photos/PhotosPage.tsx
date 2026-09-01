import { useCallback, useState } from "react";
import { Link } from "react-router";
import { Eye, Images, Search, Sparkles } from "lucide-react";

import { api, type Photo, fileUrl } from "../../api";
import PageHeader from "../../components/PageHeader";
import { useLiveQuery } from "../../live/LiveProvider";
import MoveToVault from "../vault/MoveToVault";

/**
 * Every image in the archive, as pictures.
 *
 * A list of filenames is the wrong shape for photographs. You recognise a
 * picture instantly and read a filename slowly, so a grid finds things a table
 * cannot — and it makes the gap visible: an image OCR could not read and
 * nothing has described is, in a list, indistinguishable from any other row.
 *
 * "Nothing said about these" is therefore a filter rather than a footnote. It
 * is the working set for the description pass.
 */
export default function PhotosPage() {
  const [photos, setPhotos] = useState<Photo[]>([]);
  const [total, setTotal] = useState(0);
  const [q, setQ] = useState("");
  const [undescribed, setUndescribed] = useState(false);
  const [selected, setSelected] = useState<Photo | null>(null);
  const [describing, setDescribing] = useState(false);
  const [outcome, setOutcome] = useState<string | null>(null);

  const load = useCallback(async () => {
    const wall = await api.photos({ q: q || undefined, undescribed, limit: 200 });
    setPhotos(wall.photos);
    setTotal(wall.total);
  }, [q, undescribed]);

  useLiveQuery(["documents", "files"], load);

  const unread = photos.filter((photo) => !photo.described);

  /**
   * Sending the unreadable ones back through classification.
   *
   * Nothing is overwritten here. The classify job is reset and the ordinary
   * pipeline runs; the existing classification stays until a new one succeeds,
   * so the worst case of pressing this is that nothing changes. What is
   * different this time is that a document with almost no text now goes to the
   * model as page images rather than as an empty string — so it can be
   * described by being looked at.
   */
  async function describe() {
    setDescribing(true);
    setOutcome(null);
    try {
      const result = await api.reclassify({
        document_ids: unread.map((photo) => photo.document_id),
      });
      setOutcome(
        result.queued === 0
          ? "Nothing was queued — these may already be waiting."
          : `Queued ${result.queued} image${result.queued === 1 ? "" : "s"} to be looked at.`,
      );
    } catch (caught) {
      setOutcome(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setDescribing(false);
    }
  }

  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <PageHeader icon={Images} title="Photos">
        Every image in the archive. A scan of a form belongs in Archive; this is for
        the things you recognise by looking — patches, photographs, whiteboards,
        anything OCR has nothing to say about.
      </PageHeader>

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-56 flex-1">
          <Search
            size={15}
            aria-hidden
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted"
          />
          <label htmlFor="photo-search" className="sr-only">
            Filter photos
          </label>
          <input
            id="photo-search"
            value={q}
            onChange={(event) => setQ(event.target.value)}
            placeholder="Filter by title, description or filename…"
            className="w-full rounded-lg border border-edge bg-surface py-2 pl-9 pr-3 text-sm outline-none focus:border-accent"
          />
        </div>
        <button
          type="button"
          onClick={() => setUndescribed((value) => !value)}
          className={`flex items-center gap-1.5 rounded-lg border px-3 py-2 text-sm ${
            undescribed
              ? "border-accent/60 bg-accent/10 text-accent"
              : "border-edge text-muted hover:text-neutral-100"
          }`}
        >
          <Sparkles size={14} />
          Nothing said about these
        </button>
        {unread.length > 0 && (
          <button
            type="button"
            onClick={() => void describe()}
            disabled={describing}
            className="flex items-center gap-1.5 rounded-lg bg-accent px-3 py-2 text-sm font-medium text-ink disabled:opacity-40"
          >
            <Eye size={14} />
            {describing ? "Queueing…" : `Look at ${unread.length}`}
          </button>
        )}
        <span className="text-xs text-muted">{total} images</span>
      </div>

      {outcome && (
        <p className="rounded-lg border border-edge bg-surface px-3 py-2 text-sm text-muted">
          {outcome}
        </p>
      )}

      {photos.length === 0 ? (
        <p className="rounded-xl border border-edge bg-surface p-8 text-center text-sm text-muted">
          {undescribed
            ? "Every image had something readable on it."
            : "No images match that."}
        </p>
      ) : (
        <ul className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
          {photos.map((photo) => (
            <li key={photo.document_id}>
              <button
                type="button"
                onClick={() => setSelected(photo)}
                className="group w-full overflow-hidden rounded-xl border border-edge bg-surface text-left transition-colors hover:border-accent/60"
              >
                <img
                  src={fileUrl.thumb(photo.source_file_id, photo.page)}
                  alt={photo.title ?? photo.original_filename ?? "Untitled image"}
                  loading="lazy"
                  className="aspect-square w-full bg-ink object-contain"
                />
                <span className="block px-2.5 py-2">
                  <span className="block truncate text-xs font-medium">
                    {photo.title ?? photo.original_filename ?? "Untitled"}
                  </span>
                  <span className="mt-0.5 block text-[11px] text-muted">
                    {photo.described ? (
                      photo.document_date ?? photo.received_at.slice(0, 10)
                    ) : (
                      <span className="text-amber-400">
                        {photo.text_chars === 0
                          ? "nothing readable on this"
                          : `only ${photo.text_chars} characters read`}
                      </span>
                    )}
                  </span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {selected && (
        <Lightbox
          photo={selected}
          onClose={() => setSelected(null)}
          onVaulted={() => {
            setSelected(null);
            void load();
          }}
        />
      )}
    </div>
  );
}

function Lightbox({
  photo,
  onClose,
  onVaulted,
}: {
  photo: Photo;
  onClose: () => void;
  onVaulted: () => void;
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/85 p-6"
      onClick={onClose}
    >
      <div
        className="flex max-h-full w-full max-w-5xl flex-col overflow-hidden rounded-xl border border-edge bg-surface lg:flex-row"
        onClick={(event) => event.stopPropagation()}
      >
        <img
          src={fileUrl.render(photo.source_file_id, photo.page)}
          alt={photo.title ?? "Image"}
          className="max-h-[80vh] flex-1 bg-ink object-contain"
        />
        <div className="w-full shrink-0 space-y-3 border-t border-edge p-4 lg:w-80 lg:border-l lg:border-t-0">
          <h2 className="text-sm font-medium">
            {photo.title ?? photo.original_filename ?? "Untitled"}
          </h2>
          {photo.summary ? (
            <p className="text-sm text-muted">{photo.summary}</p>
          ) : (
            <p className="rounded border border-amber-900/60 bg-amber-950/20 p-2.5 text-xs text-amber-300">
              Nothing has described this image. If OCR read no text there is
              nothing to search on — running AI review over it is what gives it a
              title and tags.
            </p>
          )}
          <dl className="grid grid-cols-[6rem_1fr] gap-y-1 text-xs">
            <dt className="text-muted">Filename</dt>
            <dd className="truncate">{photo.original_filename ?? "—"}</dd>
            <dt className="text-muted">Added</dt>
            <dd>{photo.received_at.slice(0, 10)}</dd>
          </dl>
          <div className="flex flex-wrap items-center gap-2">
            <Link
              to={`/document/${photo.document_id}/page/${photo.page}`}
              className="inline-block rounded border border-edge px-3 py-1.5 text-xs hover:border-accent/60"
            >
              Open in the viewer
            </Link>
            {/* The picture you would not want on the wall is exactly the one
                this is for, so the action belongs where you are looking at it. */}
            <MoveToVault
              documentId={photo.document_id}
              title={photo.title ?? photo.original_filename}
              onMoved={onVaulted}
              className="flex items-center gap-1.5 rounded border border-edge px-3 py-1.5 text-xs hover:border-accent/60"
            />
          </div>
        </div>
      </div>
    </div>
  );
}
