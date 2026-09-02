"""`tag_source` gains `file`, the member Python already had.

`TagSource.FILE` arrived with Phase 18 — an EXIF capture date is a fact the file
carries, not an inference — and `field_source_kind` got its matching `ALTER TYPE`
in 0027. `tag_source` did not, so `document_tag.source` and `document_asset.source`
would refuse the value with *invalid input value for enum tag_source: "file"*.
In the worker that surfaces hours later as a dead letter with an opaque error,
which is the failure mode the model-id closure in `api/models.py` exists to
prevent.

Nothing writes the value yet, so this is latent rather than broken — which is
the reason to add it now rather than during the incident.

**On a populated archive:** `ALTER TYPE ... ADD VALUE` rewrites no rows and takes
no table lock. Every existing `document_tag` and `document_asset` row keeps its
value; the type simply admits a fourth. It is instantaneous on any corpus size.

It cannot run inside a transaction, hence the autocommit block — the same shape
0024 and 0027 use.

Revision ID: 0028_tag_source_file
Revises: 0027_media_and_chunked_vault
Create Date: 2026-09-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0028_tag_source_file"
down_revision: str | None = "0027_media_and_chunked_vault"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE tag_source ADD VALUE IF NOT EXISTS 'file'")


def downgrade() -> None:
    # Postgres cannot remove an enum value, and rebuilding the type would mean
    # rewriting two link tables to drop a value no row uses. `file` stays,
    # harmlessly — the same answer 0027 gave for `field_source_kind`.
    pass
