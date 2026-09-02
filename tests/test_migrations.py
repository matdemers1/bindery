"""The migration chain, drilled against data rather than against nothing (CR-042).

`tests/conftest.py` drops the schema and runs `alembic upgrade head` on every
session, so all 29 revisions are exercised — against **zero rows**, every time.
That is the one shape of migration failure an empty schema cannot show:

- a new `NOT NULL` column with no server default,
- a unique index that collides on rows that already exist,
- a backfill whose `UPDATE` is wrong,
- a type change Postgres refuses to apply in place because the column has data.

The deployed instance holds a real ~800-document corpus and upgrades are applied
by hand (`make migrate`, REQ-114). A data-shaped failure surfaces mid-upgrade on
the live archive, with the schema half-applied and the worker refusing to start
— the state Bindery is least equipped to be in. So this file runs the drill the
operator would otherwise be running for the first time in production.

Three things are asserted, in increasing cost:

1. **Every revision is reversible and the chain is linear.** All 29 define a
   `downgrade()` and, before this file, not one was ever called.
2. **The chain round-trips on an empty database.** `head → base → head` leaves
   exactly the schema a single `upgrade head` produces. This is what exercises
   every `downgrade()` at least once.
3. **Head applies to a *populated* database.** Rows are seeded through the real
   ORM models — never hand-inserted, because a row no migration would ever have
   produced proves nothing — then the newest revisions are rolled back and
   re-applied over them.

It runs on its own database (`<db>_migration_drill`), created and dropped here,
so it cannot disturb the session schema every other test shares. Alembic is
invoked as a **subprocess** with `DATABASE_URL` overridden, which is both the
only way to point `alembic/env.py` somewhere else without mutating cached
settings underneath the running app, and the same entrypoint `make migrate`
uses.
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.config import get_settings
from api.db.enums import (
    IngestSource,
    LibraryKind,
    MembershipRole,
    ReviewState,
    SourceFileState,
)
from api.db.models import (
    AppUser,
    Correspondent,
    Document,
    DocumentTag,
    Library,
    Membership,
    Page,
    SourceFile,
    Tag,
)

REPO = Path(__file__).resolve().parent.parent

# How far back the populated drill rolls before replaying. One revision would
# only ever cover whatever landed last; three covers the window an operator
# actually skips when they let a couple of releases stack up.
ROLLBACK_STEPS = 3

# Revisions whose `downgrade()` is deliberately a no-op, each with the reason.
# This list may shrink and must never grow without an argument: an unreversible
# migration is a one-way door on a database holding irreplaceable records.
DELIBERATELY_IRREVERSIBLE = {
    "0012_backfill_backlog_flag":
        "a backfill; un-flagging would push those documents back into the "
        "review queue, which is the bug rather than the previous state",
    "0028_tag_source_file":
        "adds an enum value, and Postgres cannot remove one without rebuilding "
        "the type and rewriting two link tables to drop a value no row uses",
}


# ---------------------------------------------------------------------------
# The drill database
# ---------------------------------------------------------------------------


def _urls() -> tuple[str, str, str]:
    """`(maintenance, drill, drill_database_name)`.

    Derived from whatever database this run was pointed at, so two suites run
    in parallel against different databases get different drill databases too.
    """
    url = sa.engine.make_url(get_settings().database_url)
    name = f"{url.database}_migration_drill"
    # `str(URL)` masks the password as `***`, which reaches Postgres as a wrong
    # password rather than as an obvious mistake.
    return (
        url.set(database="postgres").render_as_string(hide_password=False),
        url.set(database=name).render_as_string(hide_password=False),
        name,
    )


def _alembic(url: str, *arguments: str) -> subprocess.CompletedProcess:
    """Run the real alembic CLI against `url`, the way `make migrate` does."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *arguments],
        cwd=REPO,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=False,
    )
    return result


def _must(url: str, *arguments: str) -> None:
    result = _alembic(url, *arguments)
    assert result.returncode == 0, (
        f"`alembic {' '.join(arguments)}` failed against the drill database:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


@pytest.fixture(scope="module")
async def drill_url():
    """A database of this suite's own, rebuilt from nothing."""
    maintenance, drill, name = _urls()
    engine = create_async_engine(maintenance, isolation_level="AUTOCOMMIT")
    async with engine.connect() as connection:
        await connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
    await engine.dispose()

    yield drill

    engine = create_async_engine(maintenance, isolation_level="AUTOCOMMIT")
    async with engine.connect() as connection:
        await connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    await engine.dispose()


async def _schema(url: str) -> dict[str, list[tuple]]:
    """Every table, column, nullability, default and index, as comparable data.

    `alembic_version` is excluded: it holds the revision id, which is the one
    thing that legitimately differs between two ways of arriving at head.
    """
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            columns = (
                await connection.execute(
                    sa.text(
                        """
                        SELECT table_name, column_name, data_type, is_nullable,
                               column_default
                        FROM information_schema.columns
                        WHERE table_schema = 'public' AND table_name <> 'alembic_version'
                        ORDER BY table_name, column_name
                        """
                    )
                )
            ).all()
            indexes = (
                await connection.execute(
                    sa.text(
                        """
                        SELECT tablename, indexname, indexdef
                        FROM pg_indexes
                        WHERE schemaname = 'public' AND tablename <> 'alembic_version'
                        ORDER BY tablename, indexname
                        """
                    )
                )
            ).all()
            enums = (
                await connection.execute(
                    sa.text(
                        """
                        SELECT t.typname, e.enumlabel
                        FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
                        ORDER BY t.typname, e.enumsortorder
                        """
                    )
                )
            ).all()
    finally:
        await engine.dispose()
    return {
        "columns": [tuple(row) for row in columns],
        "indexes": [tuple(row) for row in indexes],
        "enums": [tuple(row) for row in enums],
    }


async def _tables(url: str) -> set[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    sa.text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                    )
                )
            ).scalars().all()
    finally:
        await engine.dispose()
    return set(rows)


# ---------------------------------------------------------------------------
# 1 — the chain itself
# ---------------------------------------------------------------------------


def _script():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(Config(str(REPO / "alembic.ini")))


def test_the_revision_chain_is_linear_and_has_one_head() -> None:
    """A branch point is a migration nobody can apply without choosing."""
    script = _script()
    heads = script.get_heads()
    assert len(heads) == 1, f"the revision tree has branched: {heads}"

    revisions = list(script.walk_revisions())
    assert len(revisions) >= 29, (
        f"only {len(revisions)} revisions were read — the script directory has "
        "stopped matching the tree, and every assertion below is inspecting less "
        "than it claims"
    )
    branched = [
        revision.revision
        for revision in revisions
        if isinstance(revision.down_revision, tuple) and len(revision.down_revision) > 1
    ]
    assert not branched, f"these revisions merge two parents: {branched}"


def test_every_revision_defines_a_downgrade_that_does_something() -> None:
    """`downgrade()` is the rollback runbook, and it is never run.

    `docs/backup-and-restore.md` tells the operator to roll images back; the
    schema has to come back with them. A `downgrade` that is a bare `pass` is a
    rollback that silently leaves the database ahead of the code — unless it
    says why, which is what `DELIBERATELY_IRREVERSIBLE` is for.
    """
    import ast

    stubs: list[str] = []
    reversible: set[str] = set()
    for revision in _script().walk_revisions():
        source = Path(revision.path).read_text()
        tree = ast.parse(source)
        downgrade = next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "downgrade"
            ),
            None,
        )
        if downgrade is None:
            stubs.append(f"{revision.revision}: no downgrade() at all")
            continue
        body = [
            node
            for node in downgrade.body
            if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
        ]
        if not body or all(isinstance(node, ast.Pass) for node in body):
            stubs.append(f"{revision.revision}: downgrade() is a stub")
        else:
            reversible.add(revision.revision)

    undeclared = [
        line for line in stubs if line.split(":")[0] not in DELIBERATELY_IRREVERSIBLE
    ]
    assert not undeclared, (
        "these revisions cannot be rolled back and do not say why, so a schema "
        "rollback silently leaves the database ahead of the code:\n  "
        + "\n  ".join(undeclared)
        + "\nWrite the downgrade, or add the revision to "
        "DELIBERATELY_IRREVERSIBLE with the reason."
    )

    # The other direction. An exemption that outlives the migration it excused
    # is a hole held open for whichever revision is next given that id.
    stale = sorted(set(DELIBERATELY_IRREVERSIBLE) & reversible)
    assert not stale, (
        "these revisions now have a real downgrade; remove them from "
        f"DELIBERATELY_IRREVERSIBLE: {stale}"
    )
    missing = sorted(
        set(DELIBERATELY_IRREVERSIBLE) - {line.split(":")[0] for line in stubs} - reversible
    )
    assert not missing, (
        f"DELIBERATELY_IRREVERSIBLE names revisions that no longer exist: {missing}"
    )


# ---------------------------------------------------------------------------
# 2 — the whole chain, both ways
# ---------------------------------------------------------------------------


async def test_the_chain_round_trips_to_the_same_schema(drill_url) -> None:
    """`head → base → head`, which is the only thing that runs all 29 downgrades.

    Compared on the catalog rather than on a checksum, so a failure names the
    table and column that came back different instead of saying 'not equal'.
    """
    _must(drill_url, "upgrade", "head")
    before = await _schema(drill_url)
    assert before["columns"], "the first upgrade created no columns at all"

    _must(drill_url, "downgrade", "base")
    left_behind = await _tables(drill_url) - {"alembic_version"}
    assert not left_behind, (
        "downgrading to base left tables behind, so a rollback does not undo "
        f"what the upgrade did: {sorted(left_behind)}"
    )

    _must(drill_url, "upgrade", "head")
    after = await _schema(drill_url)

    for part in ("columns", "indexes", "enums"):
        missing = [row for row in before[part] if row not in after[part]]
        added = [row for row in after[part] if row not in before[part]]
        assert not missing and not added, (
            f"the schema after a round trip differs in {part}.\n"
            f"  lost:  {missing}\n  gained: {added}"
        )


# ---------------------------------------------------------------------------
# 3 — the drill that matters: head, applied over real rows
# ---------------------------------------------------------------------------


async def _seed(url: str) -> dict[str, uuid.UUID]:
    """A representative row per table the recent revisions touch.

    Through the ORM models, deliberately: hand-written INSERTs can produce rows
    no migration would ever have seen, and a drill over impossible data proves
    nothing about the archive.
    """
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            library = Library(name="Drill", kind=LibraryKind.PERSONAL)
            user = AppUser(
                email=f"drill-{uuid.uuid4().hex[:8]}@example.test",
                password_hash="not-a-real-hash",
            )
            session.add_all([library, user])
            await session.flush()
            session.add(
                Membership(
                    user_id=user.id, library_id=library.id, role=MembershipRole.OWNER
                )
            )

            source = SourceFile(
                library_id=library.id,
                sha256=uuid.uuid4().hex * 2,
                byte_size=4096,
                original_filename="dd214.pdf",
                ingest_source=IngestSource.WEB_UPLOAD,
                page_count=2,
                state=SourceFileState.PROCESSED,
            )
            session.add(source)
            await session.flush()
            session.add_all([
                Page(source_file_id=source.id, page_number=1, text="page one"),
                Page(source_file_id=source.id, page_number=2, text="page two"),
            ])

            who = Correspondent(
                library_id=library.id, name="Department of Defense", slug="dod"
            )
            tag = Tag(library_id=library.id, name="service", slug="service")
            session.add_all([who, tag])
            await session.flush()

            # Two documents over one file: a bundle, which is the archive's
            # normal shape and not an edge case. Two rows in one library is also
            # what makes a migration adding a unique index — 0017's shape —
            # collide here rather than in production.
            document = Document(
                library_id=library.id,
                source_file_id=source.id,
                page_start=1,
                page_end=1,
                title="Certificate of Release or Discharge",
                correspondent_id=who.id,
                review_state=ReviewState.FILED,
            )
            second = Document(
                library_id=library.id,
                source_file_id=source.id,
                page_start=2,
                page_end=2,
                title="Continuation sheet",
                review_state=ReviewState.FILED,
            )
            session.add_all([document, second])
            await session.flush()
            session.add(
                DocumentTag(document_id=document.id, tag_id=tag.id, source="human")
            )
            await session.commit()
            return {
                "library": library.id,
                "document": document.id,
                "source_file": source.id,
                "tag": tag.id,
            }
    finally:
        await engine.dispose()


async def _counts(url: str) -> dict[str, int]:
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            return {
                name: (
                    await session.execute(sa.select(sa.func.count()).select_from(model))
                ).scalar_one()
                for name, model in (
                    ("document", Document),
                    ("source_file", SourceFile),
                    ("page", Page),
                    ("tag", Tag),
                    ("document_tag", DocumentTag),
                    ("correspondent", Correspondent),
                    ("library", Library),
                    ("app_user", AppUser),
                )
            }
    finally:
        await engine.dispose()


async def test_the_newest_revisions_apply_to_a_populated_database(drill_url) -> None:
    """The drill this file exists for.

    Seed at head through the models, roll back the last few revisions, then
    replay them over rows that already exist — which is precisely what
    `make migrate` does on the live archive and what no other test has ever
    done. A `NOT NULL` column with no server default, or a unique index that
    collides on real rows, fails here and nowhere else.
    """
    _must(drill_url, "upgrade", "head")
    ids = await _seed(drill_url)
    seeded = await _counts(drill_url)
    assert seeded["document"] == 2 and seeded["page"] == 2, seeded

    _must(drill_url, "downgrade", f"-{ROLLBACK_STEPS}")

    result = _alembic(drill_url, "upgrade", "head")
    assert result.returncode == 0, (
        f"the last {ROLLBACK_STEPS} revisions cannot be applied to a database "
        "that already holds documents — which is the only kind the operator "
        "ever runs them against:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )

    after = await _counts(drill_url)
    assert after == seeded, (
        "the upgrade changed how many rows exist. Migrations may add columns "
        f"and constraints; they may not lose records.\n  before: {seeded}\n"
        f"  after:  {after}"
    )

    # And the rows are still readable *through the models*, which is the check
    # that catches a column that came back with the wrong type or the wrong
    # nullability: the ORM is what the application will use five minutes later.
    engine = create_async_engine(drill_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            document = await session.get(Document, ids["document"])
            assert document is not None, "the seeded document did not survive"
            assert document.title == "Certificate of Release or Discharge"
            assert document.page_start == 1 and document.page_end == 1
            assert document.library_id == ids["library"]
            assert document.source_file_id == ids["source_file"]
            assert document.created_at is not None
    finally:
        await engine.dispose()


async def test_the_drill_is_measuring_the_real_revision_tree(drill_url) -> None:
    """A drill against an empty chain would pass everything above in silence.

    So the database is asked what it actually ran: the stamped revision must be
    the script directory's head, and the schema must carry the tables the
    archive is made of.
    """
    _must(drill_url, "upgrade", "head")

    engine = create_async_engine(drill_url)
    try:
        async with engine.connect() as connection:
            stamped = (
                await connection.execute(sa.text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
    finally:
        await engine.dispose()

    assert stamped == _script().get_current_head(), (
        f"the drill database is stamped {stamped}, not head — every upgrade "
        "assertion above ran against a different tree than this repository's"
    )

    tables = await _tables(drill_url)
    for essential in ("document", "source_file", "page", "library", "app_user", "job"):
        assert essential in tables, f"the drill schema has no `{essential}` table"
