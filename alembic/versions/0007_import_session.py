"""Backlog import sessions.

An import of five thousand documents is a long-running operation that will be
interrupted — by a restart, a power cut, or someone closing the laptop. It has
to be resumable without reprocessing what already landed (REQ-086), which means
its state is a table, not a variable.

`import_item` is keyed on `(session_id, path)` so re-walking the same directory
converges rather than duplicating. Files are matched by content hash as well, so
a document already in the archive under a different name is recognised as a
duplicate rather than ingested twice.

Revision ID: 0007_import_session
Revises: 0006_settings
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_import_session"
down_revision: str | None = "0006_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATES = ("scanning", "dry_run", "sampling", "curating", "importing", "paused",
          "completed", "failed")
ITEM_STATES = ("pending", "sampled", "ingested", "duplicate", "skipped", "failed")


def upgrade() -> None:
    now = sa.text("now()")
    uuid_col = postgresql.UUID(as_uuid=True)

    for name, values in (("import_state", STATES), ("import_item_state", ITEM_STATES)):
        postgresql.ENUM(*values, name=name).create(op.get_bind(), checkfirst=True)

    op.create_table(
        "import_session",
        sa.Column("id", uuid_col, primary_key=True),
        sa.Column("library_id", uuid_col, nullable=False),
        sa.Column("root_path", sa.Text(), nullable=False),
        sa.Column(
            "state",
            postgresql.ENUM(*STATES, name="import_state", create_type=False),
            nullable=False, server_default="scanning",
        ),
        # Pass one classifies a stratified sample; pass two runs the rest against
        # the taxonomy a human curated in between (REQ-084).
        sa.Column("pass_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("sample_size", sa.Integer(), nullable=False, server_default="200"),
        # The dry run's findings: counts by extension, duplicates, projected cost.
        sa.Column("dry_run", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("cost_estimate", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("created_by", uuid_col),
        sa.ForeignKeyConstraint(["library_id"], ["library.id"],
                                name="fk_import_session_library_id_library"),
        sa.ForeignKeyConstraint(["created_by"], ["app_user.id"],
                                name="fk_import_session_created_by_app_user"),
    )
    op.create_index("ix_import_session_library_id", "import_session", ["library_id"])

    op.create_table(
        "import_item",
        sa.Column("id", uuid_col, primary_key=True),
        sa.Column("session_id", uuid_col, nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger()),
        sa.Column("sha256", sa.Text()),
        sa.Column(
            "state",
            postgresql.ENUM(*ITEM_STATES, name="import_item_state", create_type=False),
            nullable=False, server_default="pending",
        ),
        sa.Column("source_file_id", uuid_col),
        sa.Column("error", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(["session_id"], ["import_session.id"],
                                name="fk_import_item_session_id_import_session"),
        sa.ForeignKeyConstraint(["source_file_id"], ["source_file.id"],
                                name="fk_import_item_source_file_id_source_file"),
        # Re-walking the same tree must converge, not duplicate (REQ-086).
        sa.UniqueConstraint("session_id", "path", name="uq_import_item_session_id_path"),
    )
    op.create_index("ix_import_item_session_state", "import_item", ["session_id", "state"])

    # Batch submissions, reconciled by custom_id and never by position (REQ-054).
    op.create_table(
        "batch_submission",
        sa.Column("id", uuid_col, primary_key=True),
        sa.Column("session_id", uuid_col),
        sa.Column("provider_batch_id", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="submitted"),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("succeeded_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errored_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("usage", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(["session_id"], ["import_session.id"],
                                name="fk_batch_submission_session_id_import_session"),
        sa.UniqueConstraint("provider_batch_id", name="uq_batch_submission_provider_batch_id"),
    )

    # Which document each batch request was for. The reconciliation key.
    op.create_table(
        "batch_request",
        sa.Column("id", uuid_col, primary_key=True),
        sa.Column("submission_id", uuid_col, nullable=False),
        sa.Column("custom_id", sa.Text(), nullable=False),
        sa.Column("document_id", uuid_col, nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="submitted"),
        sa.Column("error", sa.Text()),
        sa.ForeignKeyConstraint(["submission_id"], ["batch_submission.id"],
                                name="fk_batch_request_submission_id_batch_submission"),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"],
                                name="fk_batch_request_document_id_document"),
        sa.UniqueConstraint("submission_id", "custom_id",
                            name="uq_batch_request_submission_id_custom_id"),
    )
    op.create_index("ix_batch_request_document_id", "batch_request", ["document_id"])


def downgrade() -> None:
    op.drop_table("batch_request")
    op.drop_table("batch_submission")
    op.drop_table("import_item")
    op.drop_table("import_session")
    for name in ("import_item_state", "import_state"):
        postgresql.ENUM(name=name).drop(op.get_bind(), checkfirst=True)
