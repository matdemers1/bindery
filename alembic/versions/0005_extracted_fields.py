"""Typed fields pulled from known forms (T-3.15, REQ-041).

A known form can declare extractors — a DD-214's separation date and character
of service, a title's VIN. The values live on the classification rather than on
the document because they are an *output of a particular prompt version*, and
re-running a better prompt should produce a new set beside the old one rather
than overwrite it.

Revision ID: 0005_extracted_fields
Revises: 0004_tag_removal
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_extracted_fields"
down_revision: str | None = "0004_tag_removal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "classification",
        sa.Column(
            "extracted_fields", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("classification", "extracted_fields")
