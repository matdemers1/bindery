"""What each service is running, and when it last said so (REQ-153).

The api can report its own build from its own environment. The worker cannot be
asked — it is a separate image, pulled separately, and can be a different commit
without anything looking wrong: the queue drains, the screens render, and a
stage quietly behaves like last week. So it says so itself, on startup and on
every health pass.

Revision ID: 0019_service_heartbeat
Revises: 0018_event_log_actor
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_service_heartbeat"
down_revision: str | None = "0018_event_log_actor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_heartbeat",
        sa.Column(
            "id", sa.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # Unique: this table answers "what is running", not "what has ever run".
        sa.Column("service", sa.Text(), nullable=False, unique=True),
        sa.Column("commit", sa.Text(), nullable=False),
        sa.Column("built_at", sa.Text(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("service_heartbeat")
