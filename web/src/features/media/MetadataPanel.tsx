import { Camera, Clock, MapPin, Maximize2 } from "lucide-react";

import type { MediaMetadata } from "../../api";

export function formatDuration(seconds: number | null): string | null {
  if (seconds === null || Number.isNaN(seconds)) return null;
  const whole = Math.round(seconds);
  const h = Math.floor(whole / 3600);
  const m = Math.floor((whole % 3600) / 60);
  const s = whole % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

/**
 * What the file said about itself (REQ-193, REQ-194).
 *
 * Shown, not narrated: these are facts the file carried, and the archive used
 * to throw every one of them away. A capture date that came from here is
 * marked "from the file" wherever it lands, so nobody mistakes it for a guess.
 */
export default function MetadataPanel({
  media,
  compact = false,
}: {
  media: MediaMetadata;
  compact?: boolean;
}) {
  const rows: { icon: typeof Camera; label: string; value: string }[] = [];
  const when = media.captured_at?.slice(0, 16).replace("T", " ");
  if (when) rows.push({ icon: Clock, label: "Taken", value: when });
  const camera = [media.camera_make, media.camera_model].filter(Boolean).join(" ");
  if (camera) rows.push({ icon: Camera, label: "Camera", value: camera });
  if (media.width && media.height) {
    rows.push({
      icon: Maximize2,
      label: media.kind === "video" ? "Frame" : "Size",
      value: `${media.width} × ${media.height}`,
    });
  }
  const duration = formatDuration(media.duration_seconds);
  if (duration) {
    rows.push({
      icon: Clock,
      label: "Length",
      value: media.codec ? `${duration} · ${media.codec}` : duration,
    });
  }
  if (media.latitude !== null && media.longitude !== null) {
    rows.push({
      icon: MapPin,
      label: "Where",
      value: `${media.latitude.toFixed(4)}, ${media.longitude.toFixed(4)}`,
    });
  }
  if (rows.length === 0) {
    return (
      <p className={`text-muted ${compact ? "text-[11px]" : "text-xs"}`}>
        The file carried no metadata — a scan, or a screenshot.
      </p>
    );
  }
  return (
    <dl className={`grid grid-cols-[5rem_1fr] gap-y-1 ${compact ? "text-[11px]" : "text-xs"}`}>
      {rows.map((row) => {
        const Icon = row.icon;
        return (
          <>
            <dt key={`${row.label}-dt`} className="flex items-center gap-1 text-muted">
              <Icon size={11} aria-hidden /> {row.label}
            </dt>
            <dd key={`${row.label}-dd`} className="truncate">{row.value}</dd>
          </>
        );
      })}
    </dl>
  );
}
