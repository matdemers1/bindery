"""ffprobe and Pillow against real bytes (REQ-193, REQ-194). Worker image only.

The pure parsers are tested in the default run on canned output. This is the
other half: the binaries exist, they run, and what they return survives the
parse. A generated video and a generated JPEG — nothing from the real corpus.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from api.db.enums import MediaKind
from worker import media

pytestmark = pytest.mark.slow


def _tiny_mp4(path: Path) -> None:
    """Two seconds of colour bars, the smallest thing ffmpeg will call a video."""
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
            "-t", "2", "-pix_fmt", "yuv420p", "-metadata", "creation_time=2023-04-05T14:22:10Z",
            str(path),
        ],
        check=True, timeout=60,
    )


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe is worker-only")
def test_ffprobe_reads_a_real_video(tmp_path):
    clip = tmp_path / "clip.mp4"
    _tiny_mp4(clip)

    probed = media.probe_video(clip, "clip.mp4")

    assert probed.kind is MediaKind.VIDEO
    assert 1.5 <= (probed.duration_seconds or 0) <= 2.5
    assert (probed.width, probed.height) == (320, 240)
    assert probed.codec
    assert probed.captured_at is not None and probed.captured_at.year == 2023
    assert probed.browser_playable is True
    assert "320x240" in probed.summary()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is worker-only")
def test_a_poster_frame_is_written(tmp_path):
    clip = tmp_path / "clip.mp4"
    _tiny_mp4(clip)
    poster = tmp_path / "derived" / "poster.webp"

    assert media.poster_frame(clip, poster) is True
    assert poster.stat().st_size > 0


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe is worker-only")
def test_a_file_that_is_not_a_video_is_a_loud_failure(tmp_path):
    """Invariant 8. A corrupt or misnamed file dead-letters with a reason a
    person can read, rather than becoming a silent zero-length video."""
    fake = tmp_path / "not-really.mp4"
    fake.write_bytes(b"this is a text file with a video's name")
    with pytest.raises(RuntimeError, match="ffprobe could not read"):
        media.probe_video(fake, "not-really.mp4")


def test_pillow_reads_exif_from_a_generated_jpeg(tmp_path):
    PIL = pytest.importorskip("PIL")
    from PIL import Image

    image = Image.new("RGB", (64, 48), (200, 30, 30))
    exif = Image.Exif()
    exif[271] = "Canon"
    exif[272] = "EOS R"
    # DateTimeOriginal lives in the Exif IFD, which is how a real camera writes it.
    ifd = exif.get_ifd(PIL.ExifTags.IFD.Exif)
    ifd[36867] = "2019:07:14 09:31:02"
    path = tmp_path / "shot.jpg"
    image.save(path, exif=exif)

    probed = media.read_exif(path)

    assert probed is not None
    assert probed.kind is MediaKind.IMAGE
    assert (probed.width, probed.height) == (64, 48)
    assert (probed.camera_make, probed.camera_model) == ("Canon", "EOS R")
    assert probed.captured_at is not None and probed.captured_at.year == 2019


def test_an_image_with_no_exif_is_not_an_error(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    path = tmp_path / "screenshot.png"
    Image.new("RGB", (10, 10)).save(path)

    probed = media.read_exif(path)
    assert probed is not None
    assert probed.captured_at is None
    assert (probed.width, probed.height) == (10, 10)
