"""`declined` job state, acknowledgement, and the rows that were never failures.

Three jobs on the deployed archive have sat in `dead_letter` since Phase 9: a
10×5 pixel image, a 1500×10 pixel image, and a dynamic XFA form. Each raised
`PermanentFailure` — the pipeline looked at something that is not a document and
correctly declined it — and each has been lighting a red badge that cannot be
dismissed ever since (ADR-011).

`job_state` is a native Postgres enum, and a value added by `ALTER TYPE … ADD
VALUE` cannot be used until the transaction that added it has committed.
Alembic runs the whole upgrade in one transaction, so splitting this across two
migrations does not help either — the second one is still inside the same
transaction and fails with *unsafe use of new value*. `autocommit_block` is the
tool for exactly this: it commits the enum change on its own and leaves the rest
of the migration transactional.

Matched on the error text because that is the only surviving record of *why* a
job stopped. `queue.fail` took `permanent=True`, used it to short-circuit the
retry budget, and then discarded the distinction; `repr(exc)` is what remained,
and it begins with the exception's class name.

This changes operational state, not archive data. No document, page, blob or
classification is touched, and the job rows keep their error and their history —
they move out of "failed" and into "declined", which is what happened.

`acknowledged_at` is additive and reversible and deletes nothing (REQ-090).
Acknowledging a dead letter does not retry it, hide it or remove it — it only
stops it counting toward "things still demanding attention".

Revision ID: 0024_job_declined_state
Revises: 0023_offsite_object_absent
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_job_declined_state"
down_revision: str | None = "0023_offsite_object_absent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE job_state ADD VALUE IF NOT EXISTS 'declined'")

    op.add_column(
        "job", sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        """
        UPDATE job
           SET state = 'declined'
         WHERE state = 'dead_letter'
           AND last_error LIKE 'PermanentFailure(%'
        """
    )


def downgrade() -> None:
    op.execute("UPDATE job SET state = 'dead_letter' WHERE state = 'declined'")
    op.drop_column("job", "acknowledged_at")
    # The enum value is deliberately left in place. Postgres cannot drop one
    # without rewriting the type, and a value nothing references is harmless —
    # whereas a half-applied downgrade that loses rows is not.
