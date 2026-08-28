"""Superseding document-tag links.

Undoing a classification has to be able to take back the tags it applied, and
this project's answer to "we need to remove something" is already established:
supersede it. Same shape as `document.superseded_at` — the link stays, carrying
when and why it stopped applying, so the history of what was tagged and by whom
survives the undo.

Revision ID: 0004_tag_removal
Revises: 0003_classification
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_tag_removal"
down_revision: str | None = "0003_classification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("document_tag", sa.Column("removed_at", sa.DateTime(timezone=True)))
    op.add_column(
        "document_tag", sa.Column("removed_by_event_id", postgresql.UUID(as_uuid=True))
    )
    op.create_foreign_key(
        "fk_document_tag_removed_by_event_id_audit_event",
        "document_tag", "audit_event", ["removed_by_event_id"], ["id"],
    )
    # Every read of live tags filters on this.
    op.create_index(
        "ix_document_tag_live",
        "document_tag",
        ["document_id"],
        postgresql_where=sa.text("removed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_document_tag_live", table_name="document_tag")
    op.drop_constraint(
        "fk_document_tag_removed_by_event_id_audit_event", "document_tag", type_="foreignkey"
    )
    op.drop_column("document_tag", "removed_by_event_id")
    op.drop_column("document_tag", "removed_at")
