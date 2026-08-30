"""Tag log lines with who caused them, so unscoped ones stop being global.

`event_log` was filtered by `library_id IN (visible) OR library_id IS NULL`, and
that NULL branch was a leak of a different shape from a document — not a file,
but a path, an address, or a question somebody typed. On the deployed archive it
held, among the harmless machinery:

    bindery.auth    login failed for <address> from <ip>
    bindery.import  scanned /data/inbox/household/Documents/200_Finance: 13 files
    bindery.ask     answered 'What is my policy number?' with 2 citations

Every signed-in user could read all three. The question is the worst of them: a
Q&A query is among the most revealing things a person types.

Two halves to the fix. The messages themselves stopped carrying the address and
the question — those belong in `login_attempt`, which is admin-scoped, and
nowhere respectively. And a line now records the request that produced it, so
"no library" no longer means "everyone".

Revision ID: 0018_event_log_actor
Revises: 0017_per_library_dedup
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_event_log_actor"
down_revision: str | None = "0017_per_library_dedup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "event_log", sa.Column("user_id", sa.UUID(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_event_log_user_id_app_user", "event_log", "app_user", ["user_id"], ["id"]
    )
    op.create_index("ix_event_log_user_id", "event_log", ["user_id"])
    # Existing rows keep a NULL user, which is the safe direction: they become
    # visible only if their logger is on the operational allowlist. Backfilling
    # a guess would be worse than leaving diagnostics slightly narrower.


def downgrade() -> None:
    op.drop_index("ix_event_log_user_id", table_name="event_log")
    op.drop_constraint("fk_event_log_user_id_app_user", "event_log", type_="foreignkey")
    op.drop_column("event_log", "user_id")
