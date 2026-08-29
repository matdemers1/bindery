"""OCR escalation: the page ocrmypdf declines to touch (T-1.4).

A page whose content is vector paths rather than an image makes ocrmypdf skip
it — *"no images - skipping all processing on this page to avoid losing
detail"* — and **exit 0**. The file then arrives fully processed, marked
`processed`, with no text and nothing reported anywhere.

For an archive that is the worst available outcome: not a failure you can see
and retry, but a document that is silently unfindable forever. It happened to a
real scanned authorization form, which produced 0 characters on ingest and 259
words once forced.

These tests are cheap and run in the default suite, because the input that
triggers the real thing is awkward to synthesise and the argv is the part that
silently rots.
"""

from pathlib import Path

from worker.stages.normalize import _ocr_argv, _word_count


def test_word_count_reads_the_extracted_structure() -> None:
    assert _word_count({"pages": []}) == 0
    assert _word_count({"pages": [{"lines": []}]}) == 0
    assert _word_count({"pages": [{"lines": [{"words": []}]}]}) == 0
    assert (
        _word_count(
            {
                "pages": [
                    {"lines": [{"words": [{"t": "a"}, {"t": "b"}]}]},
                    {"lines": [{"words": [{"t": "c"}]}]},
                ]
            }
        )
        == 3
    )


def test_force_ocr_replaces_skip_text_rather_than_joining_it() -> None:
    """The two flags are mutually exclusive; passing both is an ocrmypdf error."""
    normal = _ocr_argv(
        Path("in.pdf"), Path("out.pdf"), Path("side.txt"), pdfa=True, image=False
    )
    forced = _ocr_argv(
        Path("in.pdf"), Path("out.pdf"), Path("side.txt"), pdfa=True, image=False, force=True
    )

    assert "--skip-text" in normal and "--force-ocr" not in normal
    assert "--force-ocr" in forced and "--skip-text" not in forced


def test_the_escalation_keeps_everything_else_identical() -> None:
    """Only the one flag differs. A forced pass that also changed the language
    or dropped the sidecar would be a different operation wearing the same name."""
    common = dict(pdfa=True, image=True)
    normal = _ocr_argv(Path("i"), Path("o"), Path("s"), **common)
    forced = _ocr_argv(Path("i"), Path("o"), Path("s"), **common, force=True)

    assert [a for a in normal if a != "--skip-text"] == [
        a for a in forced if a != "--force-ocr"
    ]


# --------------------------------------------------------------------------
# Seeing what OCR actually read
# --------------------------------------------------------------------------


import uuid  # noqa: E402

import pytest  # noqa: E402

from api.db.enums import IngestSource, SourceFileState  # noqa: E402
from api.db.models import Page, SourceFile  # noqa: E402


@pytest.fixture
async def scanned(session, signed_in):
    _, library = await signed_in()
    source_file = SourceFile(
        library_id=library.id, sha256=uuid.uuid4().hex * 2, byte_size=100,
        original_filename="handwritten.pdf", ingest_source=IngestSource.WATCHED_FOLDER,
        page_count=3, state=SourceFileState.PROCESSED,
    )
    session.add(source_file)
    await session.flush()
    session.add_all([
        Page(source_file_id=source_file.id, page_number=1,
             text="Authorization to Contact\nand Request Personal Information"),
        # The case that matters: OCR read something, badly.
        Page(source_file_id=source_file.id, page_number=2, text="Matthevv  Dernevs"),
        Page(source_file_id=source_file.id, page_number=3, text="   "),
    ])
    await session.commit()
    return library, source_file


async def test_the_ocr_text_is_returned_verbatim(client, scanned) -> None:
    """Not cleaned up and not summarised.

    The whole point is comparing what the machine read against what is on the
    page — "Matthevv Dernevs" is the answer to "why does searching my own name
    find nothing", and tidying it away would destroy the only evidence.
    """
    _library, source_file = scanned
    response = await client.get(f"/api/files/{source_file.id}/text")
    assert response.status_code == 200, response.text
    body = response.json()

    assert [page["page_number"] for page in body["pages"]] == [1, 2, 3]
    assert body["pages"][1]["text"] == "Matthevv  Dernevs"
    # Line breaks survive: the layout is part of what you are checking.
    assert "\n" in body["pages"][0]["text"]


async def test_a_page_with_only_whitespace_counts_as_empty(client, scanned) -> None:
    _library, source_file = scanned
    body = (await client.get(f"/api/files/{source_file.id}/text")).json()

    assert body["pages"][2]["characters"] == 0
    assert body["empty_pages"] == 1
    assert body["characters"] > 0, "the file as a whole did yield text"


async def test_ocr_text_is_scoped_to_the_caller(client, session, scanned, user_factory) -> None:
    _user, elsewhere = await user_factory()
    stranger = SourceFile(
        library_id=elsewhere.id, sha256=uuid.uuid4().hex * 2, byte_size=10,
        original_filename="theirs.pdf", ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1, state=SourceFileState.PROCESSED,
    )
    session.add(stranger)
    await session.flush()
    session.add(Page(source_file_id=stranger.id, page_number=1, text="private matters"))
    await session.commit()

    response = await client.get(f"/api/files/{stranger.id}/text")
    assert response.status_code == 404
    assert "private matters" not in response.text


# --------------------------------------------------------------------------
# PDF/A conversion failures that do not use the PDF/A exit code
# --------------------------------------------------------------------------


def test_pdfa_failures_are_recognised_by_message_not_only_exit_code() -> None:
    """A real 1099 and a real W-2 dead-lettered because of this.

    Both carry a DeviceN colour space — routine in professionally printed forms.
    ocrmypdf refuses the PDF/A conversion and exits **1**, not 10, so the
    fallback that exists precisely for this never fired and two tax documents
    retried five times and gave up unsearchable.
    """
    from worker.stages.normalize import _is_pdfa_failure
    from worker.subprocess_util import CommandError

    colour = CommandError(
        ["ocrmypdf"], 1,
        "ColorConversionNeededError: The input PDF has an unusual DeviceN color "
        "space that cannot be represented in PDF/A",
    )
    ghostscript = CommandError(
        ["ocrmypdf"], 1,
        "GPL Ghostscript 10.05.1: Setting Overprint Mode to 1 not permitted in "
        "PDF/A-2, overprint mode not set",
    )
    by_code = CommandError(["ocrmypdf"], 10, "could not convert to PDF/A")

    assert _is_pdfa_failure(colour)
    assert _is_pdfa_failure(ghostscript)
    assert _is_pdfa_failure(by_code)


def test_a_genuinely_unreadable_file_is_still_a_failure() -> None:
    """The fallback must not swallow errors that have nothing to do with PDF/A.

    A file that is not a PDF should fail loudly and land on the pipeline screen,
    not be quietly retried as plain PDF and fail again for a confusing reason.
    """
    from worker.stages.normalize import _is_pdfa_failure
    from worker.subprocess_util import CommandError

    assert not _is_pdfa_failure(CommandError(["ocrmypdf"], 2, "InputFileError"))
    assert not _is_pdfa_failure(CommandError(["ocrmypdf"], 8, "EncryptedPdfError"))
    assert not _is_pdfa_failure(
        CommandError(["ocrmypdf"], 3, "MissingDependencyError: tesseract not found")
    )


# --------------------------------------------------------------------------
# The supported-format audit (T-8.14)
# --------------------------------------------------------------------------


def test_every_convertible_format_is_also_offered_by_the_importer() -> None:
    """The two lists have to agree.

    `convert.CONVERTIBLE` decides what the pipeline can turn into a PDF and
    `walker.SUPPORTED` decides what the importer will even pick up. If they
    drift, a format is either skipped despite being handled, or imported and
    then failed at OCR — both silent from the outside.
    """
    from api.backlog.walker import SUPPORTED
    from worker.convert import CONVERTIBLE

    missing = sorted(CONVERTIBLE - SUPPORTED)
    assert not missing, f"the pipeline converts these but the importer skips them: {missing}"


def test_scanned_and_office_formats_do_not_overlap() -> None:
    """A format goes to OCR directly or through LibreOffice, never both.

    An overlap would mean `_ocr_input` and `_looks_like_image` disagree about a
    file, and which branch won would depend on statement order.
    """
    from api.backlog.walker import OFFICE, SCANNED

    assert not (SCANNED & OFFICE)


def test_the_formats_the_audit_found_are_supported() -> None:
    """Named individually so removing one is a deliberate act, not a typo.

    Each of these was found in a real archive and verified to convert: the
    `.mht` was a saved pay statement, the `.pages` a proposal, and the raster
    formats went through ocrmypdf untouched.
    """
    from api.backlog.walker import SUPPORTED

    for suffix in (
        ".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic",
        ".gif", ".webp", ".bmp",
        ".doc", ".docx", ".odt", ".rtf",
        ".xls", ".xlsx", ".ods", ".csv",
        ".ppt", ".pptx", ".odp",
        ".txt", ".md",
        ".html", ".htm", ".mht", ".mhtml",
        ".pages", ".numbers", ".key",
    ):
        assert suffix in SUPPORTED, f"{suffix} should be importable"


def test_things_that_are_not_documents_stay_out() -> None:
    """Deliberate exclusions, so re-adding one is a decision rather than a slip.

    OneNote is the interesting case: it is genuinely archive material — college
    notes — and is excluded only because no converter works. LibreOffice fails
    to load it outright. That is a gap to report, not to paper over.
    """
    from api.backlog.walker import SUPPORTED

    for suffix in (
        ".one", ".onetoc2",     # no working converter
        ".psd", ".indd", ".skp",  # design sources
        ".zip", ".gz", ".rar",    # archives
        ".js", ".java", ".class", ".jar", ".json", ".xml", ".plist", ".swift",
        ".gcode", ".stl", ".3mf", ".mca",
        ".exe", ".ipa", ".wav", ".mp4",
    ):
        assert suffix not in SUPPORTED, f"{suffix} is not archive material"


def test_the_digital_text_threshold_ignores_stray_ocr_junk() -> None:
    """A scan with a few junk characters is still a scan.

    The threshold decides whether the scanner corrections run, so setting it at
    zero would disable deskew and clean for every faintly-misread page — the
    exact pages that need them most.
    """
    from worker.stages.normalize import DIGITAL_TEXT_THRESHOLD

    assert DIGITAL_TEXT_THRESHOLD >= 100


# --------------------------------------------------------------------------
# Failures from a real 500-file import
# --------------------------------------------------------------------------


def test_glyph_codes_do_not_destroy_the_whole_document() -> None:
    """A PDF with a broken font encoding cost five real documents.

    `pdftotext` emits raw glyph codes for fonts it cannot map to Unicode, and
    writes those control bytes into the XML unescaped. ElementTree then rejects
    the *entire* tree — so one unreadable word on page nine loses pages one
    through eight too.
    """
    from worker.ocr.word_boxes import parse_bbox_xhtml

    document = (
        b'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body>'
        b'<doc><page width="612" height="792">'
        b'<flow><block><line>'
        b'<word xMin="1" yMin="2" xMax="3" yMax="4">Readable</word>'
        # Exactly what a custom-encoded font produces.
        b'<word xMin="5" yMin="6" xMax="7" yMax="8">\x01\x02\x03\x04</word>'
        b'</line></block></flow>'
        b"</page></doc></body></html>"
    )

    parsed = parse_bbox_xhtml(document)
    words = [w["t"] for p in parsed["pages"] for line in p["lines"] for w in line["words"]]
    assert "Readable" in words, "the legible words must survive an illegible neighbour"


def test_tabs_and_newlines_are_still_legal() -> None:
    """Sanitizing must not eat the whitespace XML genuinely permits."""
    from worker.ocr.word_boxes import parse_bbox_xhtml

    document = (
        b'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body>\n\t'
        b'<doc><page width="612" height="792"><flow><block><line>'
        b'<word xMin="1" yMin="2" xMax="3" yMax="4">Kept</word>'
        b"</line></block></flow></page></doc></body></html>"
    )
    parsed = parse_bbox_xhtml(document)
    assert parsed["pages"][0]["lines"][0]["words"][0]["t"] == "Kept"


def test_ghostscript_giving_up_counts_as_a_pdfa_failure() -> None:
    """Reported as exit 7 with none of the colour-space wording, so the
    earlier signatures missed it and two real forms dead-lettered."""
    from worker.stages.normalize import _is_pdfa_failure
    from worker.subprocess_util import CommandError

    assert _is_pdfa_failure(
        CommandError(["ocrmypdf"], 7, "SubprocessOutputError: Ghostscript PDF/A rendering failed")
    )


def test_the_refusals_that_have_a_safe_answer() -> None:
    """ocrmypdf refuses these by default and is right to, in general.

    It is wrong for this application because of a property the rest of the
    system guarantees: the original is never modified. Everything OCR writes is
    a derived artifact beside a blob that still holds the exact bytes that
    arrived, so invalidating a signature on the copy costs nothing real — and
    refusing would leave every signed form you own unsearchable.
    """
    from worker.stages.normalize import _remedy_for
    from worker.subprocess_util import CommandError

    signed = _remedy_for(
        CommandError(["ocrmypdf"], 2, "DigitalSignatureError: Input PDF has a digital signature.")
    )
    assert signed is not None
    _key, _overrides, flags, _why = signed
    assert "--invalidate-digital-signatures" in flags



def test_an_xfa_form_is_a_permanent_failure_with_a_way_out() -> None:
    """Forcing OCR was tried on the real file and produced the same refusal.

    A dynamic XFA form carries no static content — every renderer outside Adobe
    shows a "please open this in Acrobat" placeholder — so there is nothing for
    OCR to read at any setting. The message therefore says what *would* work,
    because a dead end with no exit is not a useful thing to tell someone.
    """
    import pytest as _pytest

    from worker.stages.normalize import PermanentFailure, _remedy_for
    from worker.subprocess_util import CommandError

    with _pytest.raises(PermanentFailure, match="Acrobat"):
        _remedy_for(
            CommandError(["ocrmypdf"], 2, "InputFileError: This PDF contains dynamic XFA forms")
        )


def test_failures_with_no_safe_answer_are_still_failures() -> None:
    """The remedies must not become a blanket retry.

    A file that is not a PDF, or is encrypted, has no second attempt that would
    work — it belongs on the pipeline screen where a person can see it.
    """
    from worker.stages.normalize import _remedy_for
    from worker.subprocess_util import CommandError

    assert _remedy_for(CommandError(["ocrmypdf"], 2, "InputFileError: not a PDF")) is None
    assert _remedy_for(CommandError(["ocrmypdf"], 8, "EncryptedPdfError")) is None
    assert _remedy_for(CommandError(["ocrmypdf"], 3, "MissingDependencyError")) is None


def test_a_degenerate_image_is_refused_in_plain_language() -> None:
    """laser.png was 10x5 pixels. img2pdf handed it to pikepdf, which rejected
    the page size with a bare ValueError and a traceback naming none of it."""
    import tempfile as _tempfile
    from pathlib import Path as _Path

    import pytest as _pytest
    from PIL import Image

    from api.db.models import SourceFile
    from worker.stages.normalize import _prepare_image

    with _tempfile.TemporaryDirectory() as scratch:
        tiny = _Path(scratch) / "laser.png"
        Image.new("RGBA", (10, 5)).save(tiny)
        source_file = SourceFile(
            library_id=None, sha256="x" * 64, byte_size=1, original_filename="laser.png"
        )
        from worker.stages.normalize import PermanentFailure

        with _pytest.raises(PermanentFailure, match="too small to be a document"):
            _prepare_image(source_file, tiny)


def test_remedies_accumulate_rather_than_replace_each_other() -> None:
    """A signed PDF that then fails PDF/A needs both fixes.

    Applying them one at a time in isolation reports the second failure as if
    the first had never been solved — which is what happened to two signed
    military forms: the signature remedy worked, Ghostscript then refused the
    PDF/A conversion, and the file failed anyway.
    """
    from pathlib import Path as P

    from worker.stages.normalize import _ocr_argv, _remedy_for
    from worker.subprocess_util import CommandError

    overrides = {"pdfa": True, "image": False, "force": False, "scanned": True}
    extra: list[str] = []

    for error in (
        CommandError(["ocrmypdf"], 2, "DigitalSignatureError: Input PDF has a digital signature."),
        CommandError(["ocrmypdf"], 7, "SubprocessOutputError: Ghostscript PDF/A rendering failed"),
    ):
        _key, argv_overrides, flags, _why = _remedy_for(error)
        overrides.update(argv_overrides)
        extra += flags

    argv = _ocr_argv(P("i"), P("o"), P("s"), **overrides) + extra
    assert "--invalidate-digital-signatures" in argv
    assert "--output-type" in argv and argv[argv.index("--output-type") + 1] == "pdf"


def test_the_mutually_exclusive_ocr_modes_never_appear_together() -> None:
    """ocrmypdf refuses --force-ocr alongside --skip-text outright."""
    from pathlib import Path as P

    from worker.stages.normalize import _ocr_argv

    for force in (True, False):
        argv = _ocr_argv(P("i"), P("o"), P("s"), pdfa=True, image=False, force=force)
        assert not ("--force-ocr" in argv and "--skip-text" in argv)


def test_a_long_error_keeps_the_end_where_the_cause_is() -> None:
    """Ghostscript prints a page of font-loading before the line that matters.

    Trimming from the front kept the chatter and lost the diagnosis — a 5MB
    military PDF failed with four thousand characters of "Loading font …" and
    nothing that said why.
    """
    from worker.subprocess_util import summarize

    noise = "\n".join(f"Loading font Helvetica-{i}" for i in range(400))
    text = summarize(noise + "\nERROR: the actual cause was here")

    assert "the actual cause was here" in text
    assert "Loading font Helvetica-0" in text, "the start is still useful context"
    assert "characters omitted" in text, "and the elision is stated, not silent"


def test_a_short_error_is_left_alone() -> None:
    from worker.subprocess_util import summarize

    assert summarize("  InputFileError: not a PDF  ") == "InputFileError: not a PDF"
