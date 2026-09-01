"""What a photograph or video already knows about itself (Phase 18).

A picture knows when and where it was taken and on what; a video knows how long
it is and how big. The archive used to discard all of it and ask the model to
guess. This reads it — EXIF through Pillow, everything else through ffprobe —
and hands back one shape for both.

Two halves, deliberately separate: `parse_probe` and `parse_exif` are pure
functions over dictionaries and are tested in the default run; `probe_video`
and `read_exif` touch binaries that live only in the worker image and are
tested there.
"""

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from api.db.enums import MediaKind

log = logging.getLogger("bindery.media")

# Everything ffprobe reads. The first set plays in a browser as-is; the second
# is stored and downloadable, and the screen says so rather than showing a
# player that never starts.
BROWSER_PLAYABLE = {".mp4", ".m4v", ".mov", ".webm", ".3gp"}
VIDEO_SUFFIXES = BROWSER_PLAYABLE | {".mkv", ".avi", ".wmv", ".mts", ".m2ts", ".mpg", ".mpeg"}

# Long enough for the slowest thing a phone produces; short enough that a
# corrupt file does not hold a worker slot for an hour.
PROBE_TIMEOUT = 60
POSTER_TIMEOUT = 60


def is_video(filename: str | None, mime_type: str | None = None) -> bool:
    if mime_type and mime_type.startswith("video/"):
        return True
    return bool(filename) and Path(filename).suffix.lower() in VIDEO_SUFFIXES


def browser_playable(filename: str | None) -> bool:
    return bool(filename) and Path(filename).suffix.lower() in BROWSER_PLAYABLE


@dataclass
class Probed:
    kind: MediaKind
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    captured_at: datetime | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    codec: str | None = None
    frame_rate: float | None = None
    browser_playable: bool = True
    raw: dict = field(default_factory=dict)

    def summary(self) -> str:
        """One searchable line. For a video this *is* the page text: it is
        what makes "the clip from the lake in 2023" findable at all."""
        parts = [self.kind.value]
        if self.duration_seconds:
            m, s = divmod(int(self.duration_seconds), 60)
            parts.append(f"{m}m{s:02d}s" if m else f"{s}s")
        if self.width and self.height:
            parts.append(f"{self.width}x{self.height}")
        if self.camera_make or self.camera_model:
            parts.append(" ".join(p for p in (self.camera_make, self.camera_model) if p))
        if self.captured_at:
            parts.append(self.captured_at.strftime("%Y-%m-%d"))
        if self.codec:
            parts.append(self.codec)
        return " · ".join(parts)


# --------------------------------------------------------------------------
# Video, via ffprobe
# --------------------------------------------------------------------------


def _ratio(text: str | None) -> float | None:
    if not text or "/" not in text:
        return None
    num, den = text.split("/", 1)
    try:
        return float(num) / float(den) if float(den) else None
    except ValueError:
        return None


def _quicktime_date(text: str | None) -> datetime | None:
    """`creation_time` as ffprobe reports it: ISO 8601, usually with a Z."""
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso6709(text: str | None) -> tuple[float, float] | None:
    """`+37.7749-122.4194+010.000/` — the location tag phones write into MOV/MP4.

    Latitude, longitude, then an optional altitude. The first version split on
    the *last* sign and read the altitude as the longitude.
    """
    if not text:
        return None
    match = re.match(r"^([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)", text.strip())
    if not match:
        return None
    return float(match.group(1)), float(match.group(2))


def parse_probe(raw: dict, filename: str | None) -> Probed:
    """Pure. `raw` is ffprobe's JSON with `format` and `streams`."""
    fmt = raw.get("format", {}) or {}
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    video = next(
        (s for s in raw.get("streams", []) if s.get("codec_type") == "video"), {}
    )
    stream_tags = {k.lower(): v for k, v in (video.get("tags") or {}).items()}

    duration = fmt.get("duration") or video.get("duration")
    location = _iso6709(
        tags.get("com.apple.quicktime.location.iso6709") or tags.get("location")
    )
    width, height = video.get("width"), video.get("height")
    # A phone held upright records landscape frames with a rotation tag, and
    # the picture people see is the rotated one.
    rotation = (video.get("side_data_list") or [{}])[0].get("rotation") or stream_tags.get("rotate")
    if rotation and int(float(rotation)) % 180 != 0:
        width, height = height, width

    return Probed(
        kind=MediaKind.VIDEO,
        width=width,
        height=height,
        duration_seconds=float(duration) if duration else None,
        captured_at=_quicktime_date(
            tags.get("com.apple.quicktime.creationdate") or tags.get("creation_time")
        ),
        camera_make=tags.get("com.apple.quicktime.make") or tags.get("make"),
        camera_model=tags.get("com.apple.quicktime.model") or tags.get("model"),
        latitude=location[0] if location else None,
        longitude=location[1] if location else None,
        codec=video.get("codec_name"),
        frame_rate=_ratio(video.get("avg_frame_rate")) or _ratio(video.get("r_frame_rate")),
        browser_playable=browser_playable(filename),
        raw=raw,
    )


def probe_video(path: Path, filename: str | None) -> Probed:
    """Run ffprobe. Raises `RuntimeError` on a file it cannot read, which the
    queue records as a failure and a person sees on the Pipeline screen."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        capture_output=True, text=True, timeout=PROBE_TIMEOUT, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe could not read this file: {result.stderr.strip()[:300]}")
    return parse_probe(json.loads(result.stdout or "{}"), filename)


def poster_frame(path: Path, destination: Path, *, at_seconds: float = 1.0) -> bool:
    """One frame, as WebP, into the derived artifacts.

    A wall of blank video cards is not a wall. The frame is a derived artifact
    like a page render — reproducible from the original and purged with the
    others when the document is sealed.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-ss", str(at_seconds), "-i", str(path),
            "-frames:v", "1", "-vf", "scale=640:-2", str(destination),
        ],
        capture_output=True, text=True, timeout=POSTER_TIMEOUT, check=False,
    )
    if result.returncode != 0 and at_seconds > 0:
        # Shorter than a second: take the first frame instead.
        return poster_frame(path, destination, at_seconds=0)
    return destination.is_file()


# --------------------------------------------------------------------------
# Images, via Pillow
# --------------------------------------------------------------------------

_EXIF_DATE = 36867  # DateTimeOriginal
_EXIF_DATE_DIGITIZED = 36868
_EXIF_OFFSET = 36881  # OffsetTimeOriginal
_EXIF_MAKE = 271
_EXIF_MODEL = 272
_EXIF_GPS = 34853


def _exif_date(text: str | None, offset: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.strptime(text.strip(), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    if offset:
        try:
            return datetime.fromisoformat(parsed.isoformat() + offset)
        except ValueError:
            pass
    # No zone in the file. Recorded as UTC rather than guessed, and the display
    # shows the date, which is what anyone actually wants from it.
    return parsed.replace(tzinfo=UTC)


def _dms(values, ref: str | None) -> float | None:
    try:
        degrees = float(values[0]) + float(values[1]) / 60 + float(values[2]) / 3600
    except (TypeError, IndexError, ValueError, ZeroDivisionError):
        return None
    return -degrees if ref in ("S", "W") else degrees


def parse_exif(exif: dict, gps: dict, width: int | None, height: int | None) -> Probed:
    """Pure. `exif` and `gps` are the tag dictionaries Pillow returns."""
    lat = _dms(gps.get(2), gps.get(1)) if gps else None
    lon = _dms(gps.get(4), gps.get(3)) if gps else None
    make = exif.get(_EXIF_MAKE)
    model = exif.get(_EXIF_MODEL)
    return Probed(
        kind=MediaKind.IMAGE,
        width=width,
        height=height,
        captured_at=_exif_date(
            exif.get(_EXIF_DATE) or exif.get(_EXIF_DATE_DIGITIZED), exif.get(_EXIF_OFFSET)
        ),
        camera_make=str(make).strip() if make else None,
        camera_model=str(model).strip() if model else None,
        latitude=lat,
        longitude=lon,
        raw={
            "exif": {str(k): str(v)[:200] for k, v in exif.items()},
            "gps": {str(k): str(v)[:200] for k, v in gps.items()},
        },
    )


def read_exif(path: Path) -> Probed | None:
    """Open with Pillow (HEIC via pillow-heif, registered by normalize) and
    read what is there. None when the file has no EXIF at all — a scan, a
    screenshot — which is not an error."""
    from PIL import ExifTags, Image

    with Image.open(path) as image:
        width, height = image.size
        exif = image.getexif()
        if not exif:
            return Probed(kind=MediaKind.IMAGE, width=width, height=height, raw={})
        # Pillow keeps the camera tags in an IFD; merge so the parser sees one map.
        merged = dict(exif)
        try:
            merged.update(exif.get_ifd(ExifTags.IFD.Exif))
        except Exception:  # an IFD the file claims and does not have
            pass
        try:
            gps = dict(exif.get_ifd(ExifTags.IFD.GPSInfo))
        except Exception:
            gps = {}
    return parse_exif(merged, gps, width, height)


async def record(session, source_file_id, probed: Probed) -> None:
    """Write what the file said. Upserted: a re-run reads the file again and
    the newer reading wins, which is the one thing that may overwrite this."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from api.db.models import MediaMetadata

    values = dict(
        source_file_id=source_file_id,
        kind=probed.kind,
        width=probed.width,
        height=probed.height,
        duration_seconds=probed.duration_seconds,
        captured_at=probed.captured_at,
        camera_make=probed.camera_make,
        camera_model=probed.camera_model,
        latitude=probed.latitude,
        longitude=probed.longitude,
        codec=probed.codec,
        frame_rate=probed.frame_rate,
        browser_playable=probed.browser_playable,
        raw=probed.raw,
    )
    await session.execute(
        pg_insert(MediaMetadata)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[MediaMetadata.source_file_id],
            set_={k: v for k, v in values.items() if k != "source_file_id"},
        )
    )
