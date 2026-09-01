"""The pure halves of `worker/media.py` (REQ-193, REQ-194).

ffprobe and Pillow live only in the worker image, so the code that runs them
is tested there under `-m slow`. Everything that turns their output into a
`Probed` is a function over dictionaries and is tested here, on canned output.
"""

from datetime import UTC, datetime

from api.db.enums import MediaKind
from api.vault import store
from worker import media

IPHONE_MOV = {
    "format": {
        "duration": "133.27",
        "tags": {
            "com.apple.quicktime.creationdate": "2023-04-05T14:22:10-0700",
            "com.apple.quicktime.location.ISO6709": "+37.7749-122.4194+010.000/",
            "com.apple.quicktime.make": "Apple",
            "com.apple.quicktime.model": "iPhone 12",
        },
    },
    "streams": [
        {"codec_type": "audio", "codec_name": "aac"},
        {
            "codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080,
            "avg_frame_rate": "30000/1001",
            "side_data_list": [{"rotation": -90}],
        },
    ],
}


def test_an_iphone_video_is_read_completely():
    probed = media.parse_probe(IPHONE_MOV, "IMG_0412.MOV")
    assert probed.kind is MediaKind.VIDEO
    assert probed.duration_seconds == 133.27
    assert probed.captured_at == datetime.fromisoformat("2023-04-05T14:22:10-07:00")
    assert (probed.latitude, probed.longitude) == (37.7749, -122.4194)
    assert (probed.camera_make, probed.camera_model) == ("Apple", "iPhone 12")
    assert probed.codec == "hevc"
    assert round(probed.frame_rate, 2) == 29.97
    assert probed.browser_playable is True


def test_a_rotated_phone_video_reports_the_picture_you_see():
    """Held upright, a phone records landscape frames with a rotation tag."""
    probed = media.parse_probe(IPHONE_MOV, "IMG_0412.MOV")
    assert (probed.width, probed.height) == (1080, 1920)


def test_the_summary_is_a_searchable_line():
    """For a video this *is* the page text — what makes it findable at all."""
    line = media.parse_probe(IPHONE_MOV, "IMG_0412.MOV").summary()
    assert "2m13s" in line
    assert "1080x1920" in line
    assert "Apple iPhone 12" in line
    assert "2023-04-05" in line


def test_a_bare_file_still_parses():
    probed = media.parse_probe({"format": {}, "streams": []}, "x.avi")
    assert probed.kind is MediaKind.VIDEO
    assert probed.duration_seconds is None
    assert probed.browser_playable is False


def test_exif_from_a_camera():
    exif = {
        36867: "2019:07:14 09:31:02", 36881: "+02:00", 271: "Canon ", 272: "EOS R",
    }
    gps = {1: "N", 2: (48.0, 51.0, 29.9), 3: "E", 4: (2.0, 17.0, 40.2)}
    probed = media.parse_exif(exif, gps, 6000, 4000)
    assert probed.kind is MediaKind.IMAGE
    assert probed.captured_at == datetime.fromisoformat("2019-07-14T09:31:02+02:00")
    assert (probed.camera_make, probed.camera_model) == ("Canon", "EOS R")
    assert round(probed.latitude, 4) == 48.8583
    assert round(probed.longitude, 4) == 2.2945


def test_exif_without_a_zone_is_recorded_as_utc_rather_than_guessed():
    probed = media.parse_exif({36867: "2019:07:14 09:31:02"}, {}, 10, 10)
    assert probed.captured_at.tzinfo is UTC


def test_a_scan_with_no_exif_is_not_an_error():
    probed = media.parse_exif({}, {}, 2480, 3508)
    assert probed.captured_at is None
    assert (probed.width, probed.height) == (2480, 3508)


def test_the_api_and_the_worker_agree_on_what_a_video_is():
    """api/ may not import worker/ (the layering rule), so the vault keeps its
    own copy of the list. Two copies that drift would show a file under
    Documents in the vault and under Videos on the wall."""
    assert set(store.VIDEO_SUFFIXES) == media.VIDEO_SUFFIXES
