import { useCallback, useState } from "react";
import { Link } from "react-router";
import { Eye, Film, Images, Search, Sparkles } from "lucide-react";

import { api, type Photo, fileUrl } from "../../api";
import PageHeader from "../../components/PageHeader";
import { useLiveQuery } from "../../live/LiveProvider";
import MetadataPanel, { formatDuration } from "../media/MetadataPanel";
import MoveToVault from "../vault/MoveToVault";

type Kind = "image" | "video";

/**
 * Every image — and every video — in the archive, as pictures.
 *
 * A list of filenames is the wrong shape for photographs. You recognise a
 * picture instantly and read a filename slowly, so a grid finds things a table
 * cannot — and it makes the gap visible: an image OCR could not read and
 * nothing has described is, in a list, indistinguishable from any other row.
 *
 * Videos (Phase 18) get their own tab rather than a place in the same grid. A
 * video card is a poster frame and a duration; a photo card is the picture.
 * They are also different things to the pipeline — a video is never OCR'd or
 * classified, so "nothing said about these" does not apply to it.
 */
export default function PhotosPage() {
  // Null until chosen, so the first view can follow what is actually there:
  // an archive whose only pictures are videos should not open on an empty
  // Photos tab. Same reasoning as the vault's tabs.
  const [chosen, setChosen] = useState<Kind | null>(null);
  const [imageCount, setImageCount] = useState<number | null>(null);
  const [photos, setPhotos] = useState<Photo[]>([]);
  const [total, setTotal] = useState(0);
  const [videoCount, setVideoCount] = useState<number | null>(null);
  const [q, setQ] = useState("");
  const [undescribed, setUndescribed] = useState(false);
  const [selected, setSelected] = useState<Photo | null>(null);
  const [describing, setDescribing] = useState(false);
  const [outcome, setOutcome] = useState<string | null>(null);

  const kind: Kind =
    chosen ?? (imageCount === 0 && (videoCount ?? 0) > 0 ? "video" : "image");

  const load = useCallback(async () => {
    // Both counts every time, so each tab label says whether there is anything
    // behind it without making you click to find out — and so the default tab
    // can be decided from what is there.
    const [images, videos] = await Promise.all([
      api.photos({ kind: "image", limit: 1 }),
      api.photos({ kind: "video", limit: 1 }),
    ]);
    setImageCount(images.total);
    setVideoCount(videos.total);
    const showing: Kind =
      chosen ?? (images.total === 0 && videos.total > 0 ? "video" : "image");
    const wall = await api.photos({
      q: q || undefined,
      undescribed: showing === "image" ? undescribed : false,
      limit: 200,
      kind: showing,
    });
    setPhotos(wall.photos);
    setTotal(wall.total);
  }, [q, undescribed, chosen]);

  useLiveQuery(["documents", "files"], load);

  const unread = kind === "image" ? photos.filter((photo) => !photo.described) : [];

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
        Every image and video in the archive. A scan of a form belongs in Archive;
        this is for the things you recognise by looking — and, for videos, by
        when and how long.
      </PageHeader>

      <div className="flex gap-1 border-b border-edge">
        {(["image", "video"] as const).map((which) => {
          const Icon = which === "image" ? Images : Film;
          const count = which === "image" ? imageCount : videoCount;
          return (
            <button
              key={which}
              type="button"
              onClick={() => {
                setChosen(which);
                setSelected(null);
              }}
              className={`-mb-px flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm ${
                kind === which
                  ? "border-accent text-accent"
                  : "border-transparent text-muted hover:text-neutral-100"
              }`}
            >
              <Icon size={14} />
              {which === "image" ? "Photos" : "Videos"}
              {count !== null && <span className="text-xs text-muted">{count}</span>}
            </button>
          );
        })}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-56 flex-1">
          <Search
            size={15}
            aria-hidden
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted"
          />
          <label htmlFor="photo-search" className="sr-only">
            Filter {kind === "image" ? "photos" : "videos"}
          </label>
          <input
            id="photo-search"
            value={q}
            onChange={(event) => setQ(event.target.value)}
            placeholder="Filter by title, description or filename…"
            className="w-full rounded-lg border border-edge bg-surface py-2 pl-9 pr-3 text-sm outline-none focus:border-accent"
          />
        </div>
        {kind === "image" && (
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
        )}
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
        <span className="text-xs text-muted">
          {total} {kind === "image" ? "images" : "videos"}
        </span>
      </div>

      {outcome && (
        <p className="rounded-lg border border-edge bg-surface px-3 py-2 text-sm text-muted">
          {outcome}
        </p>
      )}

      {photos.length === 0 ? (
        <p className="rounded-xl border border-edge bg-surface p-8 text-center text-sm text-muted">
          {kind === "video"
            ? "No videos yet. Drop one in the inbox or import a folder — they are stored and described by their own metadata, never OCR'd."
            : undescribed
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
                {photo.kind === "video" ? (
                  <VideoCard photo={photo} />
                ) : (
                  <img
                    src={fileUrl.thumb(photo.source_file_id, photo.page)}
                    alt={photo.title ?? photo.original_filename ?? "Untitled image"}
                    loading="lazy"
                    className="aspect-square w-full bg-ink object-contain"
                  />
                )}
                <span className="block px-2.5 py-2">
                  <span className="block truncate text-xs font-medium">
                    {photo.title ?? photo.original_filename ?? "Untitled"}
                  </span>
                  <span className="mt-0.5 block text-[11px] text-muted">
                    {photo.kind === "video" ? (
                      photo.media?.captured_at?.slice(0, 10) ??
                      photo.document_date ??
                      photo.received_at.slice(0, 10)
                    ) : photo.described ? (
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

/** A poster frame with the duration over it; a labelled placeholder if the
 * worker could not extract one. */
function VideoCard({ photo }: { photo: Photo }) {
  const [broken, setBroken] = useState(false);
  const duration = formatDuration(photo.media?.duration_seconds ?? null);
  return (
    <span className="relative block aspect-square w-full bg-ink">
      {broken ? (
        <span className="flex h-full w-full flex-col items-center justify-center gap-2 text-muted">
          <Film size={22} aria-hidden />
          <span className="text-[11px]">no poster frame</span>
        </span>
      ) : (
        <img
          src={fileUrl.poster(photo.source_file_id)}
          alt=""
          loading="lazy"
          onError={() => setBroken(true)}
          className="h-full w-full object-cover"
        />
      )}
      <span className="absolute bottom-1.5 left-1.5 flex items-center gap-1 rounded bg-ink/80 px-1.5 py-0.5 font-mono text-[11px]">
        <Film size={11} aria-hidden /> {duration ?? "video"}
      </span>
    </span>
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
  const playable = photo.media?.browser_playable ?? true;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/85 p-6"
      onClick={onClose}
    >
      <div
        className="flex max-h-full w-full max-w-5xl flex-col overflow-hidden rounded-xl border border-edge bg-surface lg:flex-row"
        onClick={(event) => event.stopPropagation()}
      >
        {photo.kind === "video" ? (
          playable ? (
            // `FileResponse` honours Range, which is what lets this seek.
            <video
              controls
              preload="metadata"
              poster={fileUrl.poster(photo.source_file_id)}
              src={fileUrl.original(photo.source_file_id)}
              className="max-h-[80vh] flex-1 bg-ink"
            />
          ) : (
            <div className="flex flex-1 flex-col items-center justify-center gap-3 bg-ink p-8 text-center text-sm text-muted">
              <Film size={28} aria-hidden />
              <p>Browsers cannot play this format directly.</p>
              <a
                href={fileUrl.original(photo.source_file_id)}
                className="rounded-md border border-edge px-3 py-1.5 text-xs hover:border-accent/60"
              >
                Download to watch
              </a>
            </div>
          )
        ) : (
          <img
            src={fileUrl.render(photo.source_file_id, photo.page)}
            alt={photo.title ?? "Image"}
            className="max-h-[80vh] flex-1 bg-ink object-contain"
          />
        )}
        <div className="w-full shrink-0 space-y-3 border-t border-edge p-4 lg:w-80 lg:border-l lg:border-t-0">
          <h2 className="text-sm font-medium">
            {photo.title ?? photo.original_filename ?? "Untitled"}
          </h2>
          {photo.kind === "image" && (
            photo.summary ? (
              <p className="text-sm text-muted">{photo.summary}</p>
            ) : (
              <p className="rounded border border-amber-900/60 bg-amber-950/20 p-2.5 text-xs text-amber-300">
                Nothing has described this image. If OCR read no text there is
                nothing to search on — running AI review over it is what gives it a
                title and tags.
              </p>
            )
          )}
          {photo.media ? (
            <MetadataPanel media={photo.media} />
          ) : (
            <dl className="grid grid-cols-[6rem_1fr] gap-y-1 text-xs">
              <dt className="text-muted">Filename</dt>
              <dd className="truncate">{photo.original_filename ?? "—"}</dd>
              <dt className="text-muted">Added</dt>
              <dd>{photo.received_at.slice(0, 10)}</dd>
            </dl>
          )}
          <div className="flex flex-wrap items-center gap-2">
            {photo.kind === "image" && (
              <Link
                to={`/document/${photo.document_id}/page/${photo.page}`}
                className="inline-block rounded border border-edge px-3 py-1.5 text-xs hover:border-accent/60"
              >
                Open in the viewer
              </Link>
            )}
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
