"""Scope file deduplication to the library it happened in.

`source_file.sha256` was globally unique, and `get_source_file_by_hash` matched
on it globally. That was correct while Bindery had one account. It becomes a
cross-tenant leak the moment it has two, in two directions at once:

* uploading a file would match another household's row and return **their**
  `source_file` — filename, library id and all — telling you whether they
  already held those exact bytes; and
* your own upload would silently not be filed in your own library, while the
  response said it succeeded.

Found before any second account existed, which is the only comfortable time to
find it.

The blob on disk is untouched and still shared: identical bytes are one file
however many libraries record holding them, which is the point of
content-addressed storage. What becomes per-library is the *record*.

Revision ID: 0017_per_library_dedup
Revises: 0016_accounts
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_per_library_dedup"
down_revision: str | None = "0016_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The constraint name varies with how it was created; find it rather than
    # guess, because a wrong guess here fails a production migration.
    connection = op.get_bind()
    existing = connection.execute(
        sa.text(
            "select conname from pg_constraint "
            "where conrelid = 'source_file'::regclass and contype = 'u' "
            "and pg_get_constraintdef(oid) = 'UNIQUE (sha256)'"
        )
    ).scalars().all()
    for name in existing:
        op.drop_constraint(name, "source_file", type_="unique")

    op.create_index("ix_source_file_sha256", "source_file", ["sha256"])
    op.create_unique_constraint(
        "uq_source_file_library_sha256", "source_file", ["library_id", "sha256"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_source_file_library_sha256", "source_file", type_="unique")
    op.drop_index("ix_source_file_sha256", table_name="source_file")
    # Only possible if no two libraries hold the same bytes, which is the state
    # this migration existed to stop being mandatory.
    op.create_unique_constraint("uq_source_file_sha256", "source_file", ["sha256"])
