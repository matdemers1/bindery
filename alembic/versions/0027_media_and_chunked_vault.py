"""Media metadata, the `file` field source, and the chunked vault format (Phase 18).

Three things, one release:

`media_metadata` — what a photograph or video already knows about itself.
The archive discarded it and asked the model to guess (REQ-193, REQ-194).

`field_source_kind` gains `file`: a capture date read from EXIF is a fact the
file carries, not an inference. It fills `document_date` when nothing else has
and is still outranked by a person (Phase 17's rule, one more source).

`import_session.to_vault` — an import whose documents are sealed as they finish
(REQ-197).

`vault_item.format_version` — which encryption format an object is in. 1 is
the one-message format of ADR-012; 2 is the chunked format of ADR-013 that lets
a byte range be served without decrypting the whole file. Existing rows are 1
and are re-sealed to 2 the next time their vault is open.

`ALTER TYPE ... ADD VALUE` cannot run inside a transaction, hence the
autocommit block — the same shape migration 0024 used.

Revision ID: 0027_media_and_chunked_vault
Revises: 0026_field_source
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027_media_and_chunked_vault"
down_revision: str | None = "0026_field_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE field_source_kind ADD VALUE IF NOT EXISTS 'file'")

    bind = op.get_bind()
    postgresql.ENUM("image", "video", name="media_kind").create(bind, checkfirst=True)

    op.create_table(
        "media_metadata",
        sa.Column(
            "source_file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("source_file.id"),
            primary_key=True,
        ),
        sa.Column(
            "kind",
            postgresql.ENUM("image", "video", name="media_kind", create_type=False),
            nullable=False,
        ),
        sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()),
        sa.Column("duration_seconds", sa.Float()),
        sa.Column("captured_at", sa.DateTime(timezone=True)),
        sa.Column("camera_make", sa.Text()),
        sa.Column("camera_model", sa.Text()),
        sa.Column("latitude", sa.Float()),
        sa.Column("longitude", sa.Float()),
        sa.Column("codec", sa.Text()),
        sa.Column("frame_rate", sa.Float()),
        sa.Column("browser_playable", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("raw", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
    )
    op.create_index("ix_media_metadata_captured_at", "media_metadata", ["captured_at"])

    op.add_column(
        "vault_item",
        sa.Column("format_version", sa.Integer(), nullable=False, server_default="1"),
    )
    # An import bound for the vault (REQ-197): every document it produces is
    # sealed as its pipeline finishes, while the creator's vault is open.
    op.add_column(
        "import_session",
        sa.Column("to_vault", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("import_session", "to_vault")
    op.drop_column("vault_item", "format_version")
    op.drop_index("ix_media_metadata_captured_at", table_name="media_metadata")
    op.drop_table("media_metadata")
    postgresql.ENUM(name="media_kind").drop(op.get_bind(), checkfirst=True)
    # Postgres cannot remove an enum value; `file` stays, harmlessly.
