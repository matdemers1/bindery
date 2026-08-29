"""Let document types be merged away, like correspondents and tags already can.

Correspondents and tags have carried `merged_into_id` since Phase 5. Document
types never did — which meant the one kind of taxonomy the model invents most
freely was the only kind that could not be tidied afterwards. When R-08 fired,
166 of 277 types were used exactly once and there was no mechanism to collapse
them.

Tombstone rather than delete, for the same reason as everywhere else: undo is a
flag flip, historical references still resolve, and REQ-090 holds.

Revision ID: 0014_document_type_merge
Revises: 0013_classification_page_images
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_document_type_merge"
down_revision: str | None = "0013_classification_page_images"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "document_type",
        sa.Column("merged_into_id", sa.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "document_type",
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_document_type_merged_into_id_document_type",
        "document_type", "document_type",
        ["merged_into_id"], ["id"],
    )
    op.create_index(
        "ix_document_type_merged_into_id", "document_type", ["merged_into_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_document_type_merged_into_id", table_name="document_type")
    op.drop_constraint(
        "fk_document_type_merged_into_id_document_type", "document_type", type_="foreignkey"
    )
    op.drop_column("document_type", "merged_at")
    op.drop_column("document_type", "merged_into_id")
