"""`document.updated_at` and `import_session.updated_at` start telling the truth.

Both columns were declared `NOT NULL DEFAULT now()` and then never written by
anything. `api/editing.py` sets fields through `setattr` and does not touch them;
neither does `worker/stages/classify.py`, `api/moves.py`, `api/segments.py`,
`api/bulk.py` or `api/vault/store.py`. Every document in the archive therefore
reports `updated_at == created_at` forever, however many times it has been
corrected, reclassified, re-filed, moved, tagged or vaulted. A timestamp that
lies is worse than an absent one, because it will be trusted — by an incremental
export, by a differential offsite strategy, or by a person five years from now
asking when a record last changed.

A trigger rather than SQLAlchemy's `onupdate=`, because half the writes that
matter are bulk `sa.update(Document)` statements that never load an object.
`onupdate=` would leave those silently stale, which is the same class of bug
one call site at a time. The database is the only place that sees every write.

`WHEN (OLD.* IS DISTINCT FROM NEW.*)` so an UPDATE that changes nothing does not
count as a change.

**On a populated archive:** no backfill. Historical values are unknowable — the
audit log is the record of when things changed, and inventing a timestamp here
would be the same lie in a different direction. Existing rows keep
`updated_at = created_at` until something next touches them, and rows touched
after this migration are accurate from then on. Creating the function and the
two triggers takes a brief `ACCESS EXCLUSIVE` lock on `document` and
`import_session` to update the catalog; it rewrites no rows and returns in
milliseconds even on the full corpus. Any in-flight ingest transaction will wait
for that moment, so run it with the worker stopped, as `make migrate` already
assumes.

Revision ID: 0029_updated_at_triggers
Revises: 0028_tag_source_file
Create Date: 2026-09-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0029_updated_at_triggers"
down_revision: str | None = "0028_tag_source_file"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("document", "import_session")


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$
        """
    )
    for table in TABLES:
        op.execute(
            f"""
            CREATE TRIGGER {table}_set_updated_at
            BEFORE UPDATE ON {table}
            FOR EACH ROW
            WHEN (OLD.* IS DISTINCT FROM NEW.*)
            EXECUTE FUNCTION set_updated_at()
            """
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_set_updated_at ON {table}")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
