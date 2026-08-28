"""Segments and the known-form registry.

Three things this migration makes structurally impossible rather than merely
discouraged:

- **Overlapping page ranges** over one source file (REQ-032) — a GiST exclusion
  constraint, applied only to live segments so superseded history may overlap
  freely.
- **A document in a different library from its source file** (REQ-033) — a
  composite foreign key, so no call site can forget the check.
- **Losing a previous segmentation** — segments are superseded, not deleted, so
  undo (REQ-037) is a flag flip rather than a reconstruction.

Also adds a monotonic `sequence` to `audit_event`. `created_at` defaults to
`now()`, which in PostgreSQL is the *transaction* start time, so two events
written in one transaction are indistinguishable by timestamp — and "the last
segmentation" then resolves arbitrarily, which breaks undo.

Revision ID: 0002_segments
Revises: 0001_baseline
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_segments"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Needed for `source_file_id WITH =` inside a GiST exclusion constraint;
    # GiST alone has no equality operator class for uuid.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    now = sa.text("now()")

    # Monotonic ordering for the audit trail. now() is transaction-scoped, so
    # timestamps cannot separate two events written together.
    op.add_column(
        "audit_event",
        sa.Column("sequence", sa.BigInteger(), sa.Identity(always=False), nullable=False),
    )
    op.create_index("ix_audit_event_sequence", "audit_event", ["sequence"], unique=True)

    op.create_table(
        "known_form",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column(
            "match_rules", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "field_extractors", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.UniqueConstraint("code", name="uq_known_form_code"),
    )

    # The target half of the composite FK below.
    op.create_unique_constraint(
        "uq_source_file_id_library_id", "source_file", ["id", "library_id"]
    )

    op.add_column(
        "document", sa.Column("known_form_id", postgresql.UUID(as_uuid=True))
    )
    op.add_column("document", sa.Column("superseded_at", sa.DateTime(timezone=True)))
    op.add_column(
        "document", sa.Column("superseded_by_event_id", postgresql.UUID(as_uuid=True))
    )

    op.create_foreign_key(
        "fk_document_known_form_id_known_form",
        "document", "known_form", ["known_form_id"], ["id"],
    )
    op.create_foreign_key(
        "fk_document_superseded_by_event_id_audit_event",
        "document", "audit_event", ["superseded_by_event_id"], ["id"],
    )
    # REQ-033, declaratively: library_id must match the source file's.
    op.create_foreign_key(
        "fk_document_source_file_library",
        "document", "source_file",
        ["source_file_id", "library_id"], ["id", "library_id"],
    )

    op.create_index("ix_document_known_form_id", "document", ["known_form_id"])
    # Every read of live segments filters on this.
    op.create_index(
        "ix_document_live_by_source_file",
        "document",
        ["source_file_id", "page_start"],
        postgresql_where=sa.text("superseded_at IS NULL"),
    )

    # REQ-032. Inclusive on both ends: pages 1-3 and 3-5 overlap on page 3.
    op.execute("""
        ALTER TABLE document ADD CONSTRAINT ex_document_page_range_overlap
        EXCLUDE USING gist (
            source_file_id WITH =,
            int4range(page_start, page_end, '[]') WITH &&
        )
        WHERE (superseded_at IS NULL)
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE document DROP CONSTRAINT ex_document_page_range_overlap")
    op.drop_index("ix_document_live_by_source_file", table_name="document")
    op.drop_index("ix_document_known_form_id", table_name="document")
    op.drop_constraint("fk_document_source_file_library", "document", type_="foreignkey")
    op.drop_constraint(
        "fk_document_superseded_by_event_id_audit_event", "document", type_="foreignkey"
    )
    op.drop_constraint("fk_document_known_form_id_known_form", "document", type_="foreignkey")
    op.drop_column("document", "superseded_by_event_id")
    op.drop_column("document", "superseded_at")
    op.drop_column("document", "known_form_id")
    op.drop_constraint("uq_source_file_id_library_id", "source_file", type_="unique")
    op.drop_table("known_form")
    op.drop_index("ix_audit_event_sequence", table_name="audit_event")
    op.drop_column("audit_event", "sequence")
