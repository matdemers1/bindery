"""Persisted, queryable diagnostics.

Log output went to the container's stdout and nowhere else, so explaining a
failed file meant SSHing to the host and hoping the line had not scrolled away.

Deliberately **not** pruned. A few hundred bytes per job on a personal archive
is tens of megabytes over its whole life, and a scheduled delete would be the
one thing in this system that removes rows on its own — which is exactly the
property the archive is built not to have. If it ever needs trimming that is a
human's explicit decision.

Revision ID: 0011_event_log
Revises: 0010_api_tokens
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_event_log"
down_revision: str | Sequence[str] | None = "0010_api_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_log",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            primary_key=True, server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("sequence", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("logger", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("library_id", postgresql.UUID(as_uuid=True)),
        sa.Column("source_file_id", postgresql.UUID(as_uuid=True)),
        sa.Column("document_id", postgresql.UUID(as_uuid=True)),
        sa.Column("job_id", postgresql.UUID(as_uuid=True)),
        sa.Column("stage", sa.Text()),
        sa.Column("detail", sa.Text()),
        sa.Column(
            "context", postgresql.JSONB(),
            nullable=False, server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("sequence", name="uq_event_log_sequence"),
    )
    op.create_index("ix_event_log_source_file", "event_log", ["source_file_id", "sequence"])
    op.create_index("ix_event_log_level", "event_log", ["level", "sequence"])
    op.create_index("ix_event_log_library_id", "event_log", ["library_id"])


def downgrade() -> None:
    op.drop_index("ix_event_log_library_id", table_name="event_log")
    op.drop_index("ix_event_log_level", table_name="event_log")
    op.drop_index("ix_event_log_source_file", table_name="event_log")
    op.drop_table("event_log")
