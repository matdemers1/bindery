"""One boundary, applied by default, and loud when it is not (CR-013, CR-021).

`api/db/scope.py` has always *said* that no call site implements its own check.
It was not true: twenty-seven hand-written `library_id.in_(...)` clauses across
the routers said otherwise, and the test the docstring named as the thing that
kept it true did not exist. This file is that test.

The rule it enforces is one rule, not two, and that is the point. A caller that
asks the boundary for its condition — `scope.only(Document)` — gets the library
filter *and* the vault filter together, because they are assembled in one place.
A caller that writes `Document.library_id.in_(...)` by hand gets whichever half
it remembered, which is how `/api/photos` shipped without a vault clause and how
`/api/pipeline/files` was still shipping without one when this was written.

So the boundary is inverted here: the filtered path is what you get by not
thinking about it, and hand-building one fails this test on the commit that adds
it. Existing sites are grandfathered by name in `GRANDFATHERED` — by *function*
name, so a new route in an already-listed file is still caught — and the list may
only shrink, which `test_the_allow_list_does_not_outlive_the_call_sites_it_names`
enforces.

Structural, like `tests/test_layering.py`, and for the same reason: a boundary
defended by inspection is defended until the next author, and the next author is
usually the same person six months later.
"""

import ast
import contextlib
import hashlib
import textwrap
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

from api.db.enums import IngestSource, ReviewState, SourceFileState
from api.db.models import Document, Page, SourceFile

ROOT = Path(__file__).resolve().parent.parent

# The models `Scope` knows how to filter (api/db/scope.py: LIBRARY_SCOPED).
# Deliberately not every model with a `library_id`: `EventLog` and `Job` reach a
# library through their own joins and the boundary cannot express them, so
# flagging them here would teach people to add exemptions rather than to ask.
SCOPED_MODELS = frozenset({
    "Document", "SourceFile", "Tag", "Correspondent", "DocumentType", "Rule",
    "Asset", "SavedSearch", "ImportSession",
})

# The three the vault also hides. `Page` has no library of its own; it inherits
# its file's, and its file's vault state.
VAULT_MODELS = frozenset({"Document", "SourceFile", "Page"})

# Where the boundary is allowed to be written by hand, because this is where it
# is written. `scope.py` assembles it, `repository.py` asks `scope.py` for it,
# `boundary.py` is the vault half of it.
BOUNDARY_MODULES = frozenset({
    "api/db/scope.py", "api/db/repository.py", "api/vault/boundary.py",
})

# Naming any of these is proof the author was thinking about the vault. Crude on
# purpose — a function that queries documents and never mentions the vault
# cannot possibly be applying it, and crude is what survives a refactor.
VAULT_WORDS = ("document_clause", "hidden_source_file_ids", "vaulted_by", "vaulted")


def _sanctioned(node: ast.AST) -> bool:
    """`scope.only(Document)` — the one call that carries the whole boundary."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "only"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id in SCOPED_MODELS
    )


# `.in_(...)`, `== x`, `.is_(None)` — a `library_id` in a filter position. A
# `library_id` in a select list or an `order_by` claims nothing about who may
# see the row, so it is left alone.
FILTERING = ("in_", "not_in", "notin_", "is_", "is_not", "isnot", "any_", "__eq__")


def _model_library_id(node: ast.AST) -> bool:
    """`Document.library_id` — the class attribute, so a query, not a row."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "library_id"
        and isinstance(node.value, ast.Name)
        and node.value.id in SCOPED_MODELS
    )


def _hand_written_filter(node: ast.AST) -> bool:
    """A `library_id` being compared to something, in any spelling."""
    if isinstance(node, ast.Compare):
        return any(_model_library_id(side) for side in [node.left, *node.comparators])
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in FILTERING
        and _model_library_id(node.func.value)
    )


def _selects_a_vault_model(node: ast.AST) -> bool:
    """`sa.select(Document, ...)` / `select_from(SourceFile)` in any spelling."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name not in ("select", "select_from"):
        return False
    for argument in node.args:
        base = argument.value if isinstance(argument, ast.Attribute) else argument
        if isinstance(base, ast.Name) and base.id in VAULT_MODELS:
            return True
    return False


def offences(source: str, where: str) -> dict[str, str]:
    """Every hand-built boundary in one module, keyed `path::function`."""
    tree = ast.parse(source)

    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def enclosing(node: ast.AST) -> ast.AST | None:
        while node is not None:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                return node
            node = parents.get(node)
        return None

    text_of: dict[ast.AST | None, str] = {None: source}

    def body_text(function: ast.AST | None) -> str:
        if function not in text_of:
            text_of[function] = ast.unparse(function)
        return text_of[function]

    found: dict[str, str] = {}
    for node in ast.walk(tree):
        function = enclosing(node)
        key = f"{where}::{function.name if function else '<module>'}"
        if _hand_written_filter(node):
            found.setdefault(
                key,
                "filters `library_id` by hand instead of asking the boundary "
                "— use `scope.only(Model)`, which carries the vault filter too",
            )
        elif _selects_a_vault_model(node):
            text = body_text(function)
            if any(word in text for word in VAULT_WORDS):
                continue
            if any(_sanctioned(inner) for inner in ast.walk(function or tree)):
                continue
            found.setdefault(
                key,
                "selects documents, files or pages without naming the vault "
                "boundary — ask `Scope`/`repository`, or say why it is exempt",
            )
    return found


def scan() -> tuple[dict[str, str], int]:
    """The whole `api/` package. Returns the offences and how many files it read."""
    found, read = _scan()
    return found, len(read)


def _scan() -> tuple[dict[str, str], list[str]]:
    """As `scan`, but naming the modules rather than counting them."""
    found: dict[str, str] = {}
    read: list[str] = []
    for path in sorted((ROOT / "api").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if relative in BOUNDARY_MODULES:
            continue
        read.append(relative)
        found.update(offences(path.read_text(), relative))
    return found, read


# The modules that build document queries outside `api/routers/`, which the
# router-shaped guard in `tests/test_vault_leak.py` structurally cannot see
# (CR-039). `api/search/query.py` is the one that matters most: it is the
# archive's primary retrieval surface, it reads page *text*, and it is reached
# through a router that never mentions `Document` at all — so a guard that
# globs `api/routers/*.py` filters it out in silence.
#
# Counting modules is not enough on its own: `examined > 50` stays true while
# any fifty files are read, including fifty that build no queries. These are
# named, so a reorganisation that moves retrieval out from under this scan
# fails here instead of quietly halving what the guard covers.
MUST_BE_SCANNED = (
    "api/ask.py",
    "api/search/query.py",
    "api/reclassify.py",
    "api/bulk.py",
    "api/moves.py",
    "api/entities.py",
    "api/segments.py",
    "api/taxonomy_health.py",
    "api/health_panel.py",
    "api/export/archive_export.py",
    "api/vault/sweep.py",
    "api/routers/search.py",
    "api/routers/library.py",
    "api/routers/logs.py",
    "api/routers/review.py",
)


# Every call site that predates the guard, with the reason it is still here.
# This list may shrink and must never grow: a new entry means a new boundary
# written by hand, which is the thing the file exists to stop.
GRANDFATHERED = {
    "api/backlog/dryrun.py::analyse":
        "a dry run over a folder on disk, before any row is a document",

    "api/bulk.py::_resolve_tags":
        "bulk edit, applied to ids the caller already reached",
    "api/bulk.py::apply":
        "bulk edit, applied to ids the caller already reached",

    "api/cli.py::_enqueue_stage":
        "the operator CLI; it runs as the machine, not as a person",

    "api/entities.py::asset_timeline":
        "taxonomy service; migrating it is its own change",
    "api/entities.py::merge_correspondents":
        "taxonomy service; migrating it is its own change",
    "api/entities.py::merge_document_types":
        "taxonomy service; migrating it is its own change",
    "api/entities.py::preview_correspondent_merge":
        "taxonomy service; migrating it is its own change",
    "api/entities.py::resolve_correspondent":
        "taxonomy service; migrating it is its own change",

    "api/export/archive_export.py::collect":
        "the export walks what the caller can see, once",
    "api/export/archive_export.py::vaulted_count":
        "counts vaulted rows on purpose, to report them",

    "api/export/integrity.py::check":
        "the integrity check reads every row, vaulted included",

    "api/forms/registry.py::_page_text":
        "known-form matching, keyed on one file being ingested",

    "api/health_panel.py::collect":
        "counts jobs and files for the health panel",

    "api/moves.py::move":
        "a move rewrites `library_id`; it is the boundary changing",
    "api/moves.py::plan":
        "the preview of that move, over the same rows",

    "api/quota.py::usage_by_account":
        "counts bytes per library, for the quota",
    "api/quota.py::usage_for":
        "counts bytes per library, for the quota",

    "api/reclassify.py::pending":
        "re-runs classification over rows the caller chose",
    "api/reclassify.py::requeue":
        "re-runs classification over rows the caller chose",

    "api/routers/badges.py::badge":
        "landed in the same review round as this guard",

    "api/routers/entities.py::add_alias":
        "taxonomy screens; a batch of six, migrated together or not at all",
    "api/routers/entities.py::list_assets":
        "taxonomy screens; a batch of six, migrated together or not at all",
    "api/routers/entities.py::list_correspondents":
        "taxonomy screens; a batch of six, migrated together or not at all",
    "api/routers/entities.py::list_document_types":
        "taxonomy screens; a batch of six, migrated together or not at all",
    "api/routers/entities.py::list_shelves":
        "taxonomy screens; a batch of six, migrated together or not at all",
    "api/routers/entities.py::list_tags":
        "taxonomy screens; a batch of six, migrated together or not at all",

    "api/routers/imports.py::_owned":
        "backlog import scoping, over sessions rather than documents",
    "api/routers/imports.py::_undoable":
        "backlog import scoping, over sessions rather than documents",
    "api/routers/imports.py::list_imports":
        "backlog import scoping, over sessions rather than documents",

    "api/routers/review.py::review_queue":
        "carries both halves by hand today; a substitution, not yet made",

    "api/routers/rules.py::_owned_rule":
        "rules are library-scoped and never vaulted",
    "api/routers/rules.py::dry_run":
        "rules are library-scoped and never vaulted",
    "api/routers/rules.py::list_rules":
        "rules are library-scoped and never vaulted",

    "api/routers/trust.py::vital_records":
        "carries both halves by hand since CR-007; owned by another change",

    "api/rules.py::enabled_rules":
        "reads the rules of one library, to run them",
    "api/rules.py::facts_for":
        "reads the pages of the document a rule is being run against",

    "api/search/query.py::suggest":
        "the offending clause is `Tag.library_id.is_(None)` — global tags, "
        "which belong to no library and are deliberately widened in",

    "api/segments.py::_count_pages":
        "segment arithmetic over one file the caller already reached",
    "api/segments.py::list_segments":
        "segment arithmetic over one file the caller already reached",

    "api/taxonomy_health.py::detect_duplicates":
        "taxonomy similarity within one library",
    "api/taxonomy_health.py::find_similar":
        "taxonomy similarity within one library",
    "api/taxonomy_health.py::report":
        "taxonomy similarity within one library",

    "api/vault/store.py::_refuse_if_blob_is_shared":
        "the seal path; vaulted rows are what it operates on",
}


def _allowed(key: str) -> bool:
    return key in GRANDFATHERED


# ---------------------------------------------------------------------------
# The guard, tested on itself first. A guard whose own detection is untested is
# the guard that passed while examining nothing (Phase 7 learned this).
# ---------------------------------------------------------------------------


def test_it_catches_a_new_route_that_hand_writes_the_library_filter() -> None:
    source = textwrap.dedent(
        """
        import sqlalchemy as sa
        from api.db.models import Document

        async def a_new_screen(session, library_ids):
            return await session.execute(
                sa.select(Document).where(Document.library_id.in_(library_ids))
            )
        """
    )
    found = offences(source, "api/routers/new.py")
    assert "api/routers/new.py::a_new_screen" in found


def test_it_catches_a_new_route_that_forgets_the_vault() -> None:
    """The `/api/photos` shape: a library filter, no vault clause."""
    source = textwrap.dedent(
        """
        import sqlalchemy as sa
        from api.db.models import SourceFile

        async def a_new_wall(session, ids):
            return await session.execute(sa.select(SourceFile).where(SourceFile.id.in_(ids)))
        """
    )
    found = offences(source, "api/routers/new.py")
    assert "api/routers/new.py::a_new_wall" in found
    assert "vault" in found["api/routers/new.py::a_new_wall"]


def test_it_leaves_a_route_that_asks_the_boundary_alone() -> None:
    source = textwrap.dedent(
        """
        import sqlalchemy as sa
        from api.db.models import Document

        async def a_new_screen(session, user, scope):
            return await session.execute(sa.select(Document).where(scope.only(Document)))
        """
    )
    assert offences(source, "api/routers/new.py") == {}


def test_no_call_site_outside_the_allow_list_writes_its_own_boundary() -> None:
    found, _ = scan()
    new = {key: why for key, why in found.items() if not _allowed(key)}
    assert not new, (
        "these build the access boundary by hand rather than asking for it:\n  "
        + "\n  ".join(f"{key}: {why}" for key, why in sorted(new.items()))
        + "\nAsk `Scope` (`scope.only(Model)`, `scope.documents()`) or "
        "`repository`, which apply the library filter and the vault filter "
        "together. If this site genuinely cannot, add it to GRANDFATHERED with "
        "a reason — and expect to be asked why."
    )


def test_the_allow_list_does_not_outlive_the_call_sites_it_names() -> None:
    """It may only shrink. A stale entry is a hole left open for the next author."""
    found, _ = scan()
    stale = sorted(set(GRANDFATHERED) - set(found))
    assert not stale, (
        "these no longer build their own boundary; remove them from "
        "GRANDFATHERED so the next one that does is caught:\n  " + "\n  ".join(stale)
    )


def test_the_guard_examined_the_whole_api_package() -> None:
    """Its first version passed while examining none. This is that assertion."""
    _, examined = scan()
    assert examined > 50, f"only {examined} modules were read"


def test_the_scan_reaches_the_modules_that_actually_build_the_queries() -> None:
    """A count is not coverage (CR-039).

    The vault-boundary guard in `tests/test_vault_leak.py` examines
    `api/routers/*.py` and, after its filters, five files — none of which is
    `api/search/query.py`, `api/ask.py` or `api/reclassify.py`, which are where
    documents are actually selected. This scan does reach them; that it still
    does is asserted here by name rather than inferred from `examined > 50`,
    which stays true however the retrieval code is rearranged.
    """
    _, read = _scan()
    missing = [module for module in MUST_BE_SCANNED if module not in read]
    assert not missing, (
        "these modules build document queries and are no longer being scanned, "
        "so nothing is watching the boundary in them:\n  " + "\n  ".join(missing)
        + "\nIf one was genuinely removed, take it out of MUST_BE_SCANNED; if it "
        "moved, follow it."
    )


# ---------------------------------------------------------------------------
# The default, inverted. These call the search layer the way a new caller would
# — without saying anything about the vault — and expect it to be applied.
# ---------------------------------------------------------------------------

SECRET_PHRASE = "quetzalcoatlus northropi settlement"


@pytest.fixture
async def a_vaulted_document(session, signed_in):
    user, library = await signed_in()
    source = SourceFile(
        library_id=library.id,
        sha256=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        byte_size=2048,
        original_filename="the-private-one.jpg",
        ingest_source=IngestSource.WEB_UPLOAD,
        page_count=1,
        state=SourceFileState.PROCESSED,
    )
    session.add(source)
    await session.flush()
    session.add(
        Page(source_file_id=source.id, page_number=1, text=f"{SECRET_PHRASE} appears here")
    )
    document = Document(
        library_id=library.id,
        source_file_id=source.id,
        page_start=1,
        page_end=1,
        title=f"Private — {SECRET_PHRASE}",
        review_state=ReviewState.FILED,
        vaulted_by=user.id,
    )
    session.add(document)
    await session.commit()
    return user, library, document, source


async def test_search_hides_the_vault_from_a_caller_that_said_nothing(
    session, a_vaulted_document
) -> None:
    """The fail-open default was the defect: `viewer=None` meant `no filter`."""
    from api.search import query as search_query

    _user, library, _document, _source = a_vaulted_document
    response = await search_query.search(session, SECRET_PHRASE, [library.id])
    assert response.total == 0, "a caller that forgot the viewer was shown vaulted text"


async def test_suggest_hides_a_vaulted_title_from_a_caller_that_said_nothing(
    session, a_vaulted_document
) -> None:
    from api.search import query as search_query

    _user, library, _document, _source = a_vaulted_document
    names = await search_query.suggest(session, "quetzalcoatlus", [library.id])
    assert not [name for name in names if SECRET_PHRASE in (name or "")]


async def test_the_pipeline_file_list_does_not_carry_a_vaulted_file(
    client, a_vaulted_document
) -> None:
    """The second route that never gained the clause. The filename is the leak."""
    response = await client.get("/api/pipeline/files")
    assert response.status_code == 200, response.text
    assert "the-private-one" not in response.text


async def test_the_archive_stats_do_not_count_a_vaulted_file(
    client, a_vaulted_document
) -> None:
    """A hidden row that still moves a total says there is something here."""
    body = (await client.get("/api/archive")).json()
    assert body["stats"]["files"] == 0
    assert body["stats"]["pages"] == 0


# ---------------------------------------------------------------------------
# Opening a document (CR-031)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _statements():
    """Every SQL string the app sends while the block runs."""
    from api.db.session import engine

    captured: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        captured.append(statement)

    sa.event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield captured
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", record)


async def test_opening_a_document_does_not_read_the_rest_of_the_bundle(
    session, client, signed_in
) -> None:
    """Two pages of a twelve-page bundle, and none of its OCR text.

    The viewer shows `{page_number, render_path, thumb_path}`. Loading `Page`
    as an entity brought back `text` and the persisted `text_tsv` for every page
    of the file — megabytes on a service-records bundle, discarded in Python.
    """
    _user, library = await signed_in()
    source = SourceFile(
        library_id=library.id,
        sha256=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        byte_size=4096,
        original_filename="service-records.pdf",
        ingest_source=IngestSource.WEB_UPLOAD,
        page_count=12,
        state=SourceFileState.PROCESSED,
    )
    session.add(source)
    await session.flush()
    for number in range(1, 13):
        session.add(
            Page(
                source_file_id=source.id,
                page_number=number,
                text=f"page {number} " + "lorem ipsum " * 200,
                render_path=f"renders/{number}.webp",
                thumb_path=f"thumbs/{number}.webp",
            )
        )
    document = Document(
        library_id=library.id,
        source_file_id=source.id,
        page_start=3,
        page_end=4,
        title="DD-214",
        review_state=ReviewState.FILED,
    )
    session.add(document)
    await session.commit()

    with _statements() as captured:
        response = await client.get(f"/api/documents/{document.id}")

    assert response.status_code == 200, response.text
    assert [page["page_number"] for page in response.json()["pages"]] == [3, 4]

    page_reads = [statement for statement in captured if "FROM page" in statement]
    assert page_reads, "no page query ran at all"
    assert not [statement for statement in page_reads if "page.text" in statement], (
        "the page query still selects OCR text (and its tsvector) to render "
        "three scalars:\n  " + "\n  ".join(page_reads)
    )
    assert any("BETWEEN" in statement for statement in page_reads), (
        "the page range is still being filtered in Python rather than in SQL:\n  "
        + "\n  ".join(page_reads)
    )
