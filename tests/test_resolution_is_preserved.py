"""REQ-012 — the original image resolution survives normalization (BND-FR-009).

This requirement is satisfied by *omission*: nothing in `_ocr_argv` asks
ocrmypdf to rewrite the page image, so the page that comes out is the page that
went in. Which is the whole problem — a property held by the absence of a flag
is one nobody can see, and the flags that would break it read as improvements.
`--optimize 3` is what you reach for when the archive is large. `--clean-final`
is one character from the `--clean` that is already there, and the difference
between them is exactly this requirement: `--clean` cleans the image fed to
Tesseract, `--clean-final` writes the cleaned image into the output.

So it is asserted two ways. The argv check is the one that will actually fire,
because a flag is what a future author adds. The pixel check is the one that
proves the argv check is about the right thing, and it runs where the OCR
toolchain lives.

There is no rollback for this. An archive re-OCR'd at half resolution has lost
the detail permanently — the originals are untouched (invariant 1), but every
derived page, every thumbnail and every word box would have to be rebuilt, and
nobody would notice for months.
"""

import subprocess
from pathlib import Path

import pytest

from worker.stages.normalize import _ocr_argv

# Every ocrmypdf flag that rewrites the page image rather than the copy handed
# to Tesseract. Named individually rather than allow-listed, because the failure
# to catch is an *addition*, and a reviewer reading a new flag here is the point.
REWRITES_THE_PAGE = (
    "--clean-final",      # --clean's twin, and writes the cleaned image out
    "--optimize",         # 2 and 3 recompress images; 1 is lossless, still a rewrite
    "-O",                 # the short spelling of the same thing
    "--oversample",       # rasterizes at a different DPI
    "--remove-background",
    "--max-image-mpixels",  # a ceiling is a downscale by another name
)


@pytest.mark.parametrize("scanned", [True, False])
@pytest.mark.parametrize("image", [True, False])
@pytest.mark.parametrize("force", [True, False])
def test_no_invocation_asks_ocrmypdf_to_rewrite_the_page(scanned, image, force) -> None:
    """Across every combination, including the escalations.

    `--force-ocr` is the one worth the parametrize: it is the path that *does*
    rasterize, taken when a page has no text layer at all, and it is the obvious
    place for someone to add a resolution flag while they are already there.
    """
    argv = _ocr_argv(
        Path("in.pdf"), Path("out.pdf"), Path("out.txt"),
        pdfa=True, image=image, force=force, scanned=scanned,
    )

    offending = [flag for flag in argv if flag in REWRITES_THE_PAGE]
    assert not offending, (
        f"{offending} rewrites the page image, so the original resolution does "
        "not survive (REQ-012). If this is deliberate, it needs an ADR and a "
        "re-OCR plan — the detail is not recoverable from the derived pages."
    )


def test_clean_is_the_one_that_does_not_write_the_image_back() -> None:
    """The distinction the requirement actually turns on.

    `--clean` and `--clean-final` differ by one word and by whether the archive
    keeps its resolution. Asserting that `--clean` is the spelling in use is
    what makes the list above a real check rather than a list of words.
    """
    from api.config import get_settings

    if not get_settings().ocr_clean:
        pytest.skip("cleaning is off in this configuration")

    argv = _ocr_argv(
        Path("in.pdf"), Path("out.pdf"), Path("out.txt"),
        pdfa=True, image=False, force=False, scanned=True,
    )
    assert "--clean" in argv
    assert "--clean-final" not in argv


@pytest.mark.slow
def test_the_page_image_comes_out_at_the_pixel_size_it_went_in(tmp_path) -> None:
    """The behavioural half, against the real toolchain.

    Runs in the worker image, where ocrmypdf and Tesseract live, and reads the
    *embedded image* rather than the page geometry — page size in points can be
    unchanged while the image inside it has been resampled to a third of its
    detail, which is exactly the failure this requirement is about.

    It is what stops the argv check above from being a test about a list of
    strings: if `REWRITES_THE_PAGE` named the wrong flags, this would still
    notice the day one of them was added.
    """
    import pikepdf
    from PIL import Image

    from tests.corpus.fixtures import render_text_page

    source = render_text_page("Meridian Credit Union statement", tmp_path / "page.png")
    with Image.open(source) as original:
        before = original.size

    output = tmp_path / "out.pdf"
    subprocess.run(
        _ocr_argv(source, output, tmp_path / "out.txt", pdfa=True, image=True),
        check=True,
        capture_output=True,
    )

    with pikepdf.open(output) as pdf:
        images = [
            pikepdf.PdfImage(image)
            for page in pdf.pages
            for image in page.get_images().values()
        ]
        after = [(image.width, image.height) for image in images]

    assert after, "the output has no embedded image at all, which is a different bug"
    assert before in after, (
        f"the page went in at {before} and came out at {after} — the original "
        "resolution did not survive normalization (REQ-012). There is no "
        "rollback: the derived pages, thumbnails and word boxes would all have "
        "to be rebuilt, and nobody would notice for months."
    )
