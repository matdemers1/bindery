"""Record that an object the ledger claims is no longer in the bucket (T-13.10).

`reconcile` already noticed. It set `verified_at` to NULL and its docstring said
that was "what makes the next sync re-upload it" — and it was not, because
`_already_shipped` selected every ledger row with a blob key regardless of that
column. The object stayed missing, the ledger stayed confident, and the sync
that was supposed to replace it skipped it.

`verified_at` cannot carry this meaning on its own: NULL is also the state of a
row that was uploaded a moment ago and has never been reconciled. So absence
gets a column of its own, and the sync skips only what is both recorded *and*
not known to be gone.

Nothing is deleted, as ever (REQ-090). A row that comes back is cleared rather
than removed, so the history of an object that went missing and returned is
still there to read.

Revision ID: 0023_offsite_object_absent
Revises: 0022_offsite_run
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_offsite_object_absent"
down_revision: str | None = "0022_offsite_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "offsite_object",
        sa.Column("absent_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("offsite_object", "absent_at")
