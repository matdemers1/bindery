"""Who last set each document field (Phase 17, REQ-188, REQ-191).

One table. It answers the question every classification run asks about every
field: **may I write this?**

A field a person set is theirs. Without this, correcting a date and then letting
AI review run three days later silently puts the wrong date back — nothing
failed, nothing was logged, and the archive quietly disagrees with you. That
silent revert, not a bad edit, is the failure this phase exists to prevent.

Deliberately not part of `field_provenance`. That table hangs off a
`classification_id` and records the page and snippet justifying an AI value:
provenance of *evidence*. This is provenance of *authority*, and a document can
have excellent evidence for a value a person has since overruled.

`field_name` is text rather than an enum because which fields are editable is a
product decision that will move, and an enum would make adding one a migration.

Revision ID: 0026_field_source
Revises: 0025_private_vault
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0026_field_source"
down_revision: str | None = "0025_private_vault"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KIND = ("ai", "rule", "human")


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(*KIND, name="field_source_kind").create(bind, checkfirst=True)

    op.create_table(
        "field_source",
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("document.id"),
            nullable=False,
        ),
        sa.Column("field_name", sa.Text(), nullable=False),
        sa.Column(
            "source",
            postgresql.ENUM(*KIND, name="field_source_kind", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "set_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("app_user.id")
        ),
        sa.Column(
            "set_by_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("audit_event.id"),
        ),
        sa.Column(
            "set_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        # A claim given back is released, never deleted (REQ-090). Live claims
        # are `released_at IS NULL`, the same shape as document_tag.removed_at.
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column(
            "released_by_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("audit_event.id"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("document_id", "field_name", name="pk_field_source"),
    )
    op.create_index("ix_field_source_document_id", "field_source", ["document_id"])

    # Deliberately no backfill. Every existing value was written by
    # classification or a rule, and marking them `ai` would be a guess dressed
    # as a record — some were set by rules, and the audit log knows which.
    # An absent row means "nobody has claimed this field", which is exactly
    # right for a document no person has corrected, and lets classification
    # carry on writing it.


def downgrade() -> None:
    op.drop_index("ix_field_source_document_id", table_name="field_source")
    op.drop_table("field_source")
    postgresql.ENUM(name="field_source_kind").drop(op.get_bind(), checkfirst=True)
