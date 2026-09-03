"""Four indexes for four read paths that had none (CR-106, CR-107, CR-111, CR-112).

Each one serves a predicate that is already written and already hot; none of
them changes a single row, a constraint or a shape.

`ix_document_tag_tag_live` — the edit panel's tag picker counts documents per
tag, and the only selective predicate is `document_tag.tag_id`. The primary key
is `(document_id, tag_id)` in that order and `ix_document_tag_live` leads on
`document_id` too, so nothing was prefixed on `tag_id` and every tag's count
scanned the whole link table. Correspondents and document types have had their
equivalents since 0003; tags are the taxonomy that was missed. Partial on
`removed_at IS NULL` because a revoked link is history — the same live/history
split `ix_document_tag_live` uses — and it serves the reverse lookup
("everything tagged banking") as well as the count.

`ix_source_file_extension` — the photo wall's mandatory predicate was ten
`original_filename ILIKE '%.jpg'` patterns ORed together, which no index can
serve, over `document ⨝ source_file`, twice per load, refreshed by pipeline
activity. `api/routers/photos.py` now asks the same question as an equality
test on the filename's final suffix, and this indexes that expression. The
pattern is spelled identically in both places on purpose: Postgres matches an
expression index by the parsed expression, so a difference here silently costs
the index rather than failing.

`ix_event_log_message_trgm` — the log viewer's `q` is
`message ILIKE '%<q>%'`, a leading wildcard over a table that is deliberately
never pruned (invariant 3 governs documents; this is diagnostics, and the
answer to its growth is an index, not a retention job). Seven stages times a
twenty-thousand-file backlog is ~280K rows from the start/finish lines alone,
and the Logs screen refetches on both the `logs` and `jobs` topics. `pg_trgm`
is installed by 0001 and this is what it is for.

`ix_classification_created_at` — the health panel's spend figure is a 30-day
window over `classification`, whose indexes were `document_id` and
`prompt_version` only. The panel is refreshed by job transitions, so the row
count and the refresh rate grow together: a backlog import producing 20,000
classifications is exactly when the sequential scan runs most often.

**On a running system:** all four are built `CONCURRENTLY`, so no write is
blocked and the archive stays readable and writable throughout. The cost is
that each takes two passes over its table and cannot run inside a transaction,
hence the autocommit block — the same shape 0024, 0027 and 0028 use. A build
that is interrupted leaves an INVALID index behind, which is inert: re-running
this revision drops nothing and `IF NOT EXISTS` will skip it, so an interrupted
upgrade is repaired by `DROP INDEX CONCURRENTLY` on the invalid name and
running it again. On the deployed ~500-file corpus every one of these completes
in well under a second; the sizing note is for the archive this is meant to
grow into.

Revision ID: 0030_read_path_indexes
Revises: 0029_updated_at_triggers
Create Date: 2026-09-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0030_read_path_indexes"
down_revision: str | None = "0029_updated_at_triggers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept as one list so the upgrade and the downgrade cannot drift: a revision
# that creates four indexes and drops three leaves the round-trip drill in
# `tests/test_migrations.py` red, which is the point of it.
INDEXES: tuple[tuple[str, str], ...] = (
    (
        "ix_document_tag_tag_live",
        "ON document_tag (tag_id, document_id) WHERE removed_at IS NULL",
    ),
    (
        "ix_source_file_extension",
        r"ON source_file (lower(substring(original_filename, '\.[^.]*$')))",
    ),
    (
        "ix_event_log_message_trgm",
        "ON event_log USING gin (message gin_trgm_ops)",
    ),
    ("ix_classification_created_at", "ON classification (created_at)"),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, definition in INDEXES:
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} {definition}"
            )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _ in INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
