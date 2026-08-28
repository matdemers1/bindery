"""Scoped, revocable API tokens.

Only the hash is stored: the token is shown once and is unrecoverable, so a
stolen database backup is not a set of working credentials.

Tokens are revoked, never deleted (REQ-090) — the record of what existed and
what it could reach is part of the audit trail.

Revision ID: 0010_api_tokens
Revises: 0009_deferrable_library_fk
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_api_tokens"
down_revision: str | Sequence[str] | None = "0009_deferrable_library_fk"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_token",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            primary_key=True, server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app_user.id"), nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("prefix", sa.Text(), nullable=False),
        sa.Column(
            "scopes", postgresql.ARRAY(sa.Text()),
            nullable=False, server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "library_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False, server_default=sa.text("'{}'"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_api_token_user_id", "api_token", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_api_token_user_id", table_name="api_token")
    op.drop_table("api_token")
