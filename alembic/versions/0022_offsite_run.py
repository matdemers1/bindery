"""Every attempt to get a copy out of the building (T-13.6, REQ-163, REQ-164).

Deliberately not a `JobStage`. The queue's idempotency key is
`(source_file_id, document_id, stage, prompt_version)` with
`postgresql_nulls_not_distinct`, and a global job has all four null — so the
first enqueue would insert and **every enqueue after it would silently return
None, forever**, whether the first run succeeded, failed or dead-lettered. The
queue would look healthy while nothing left the building. See ADR-010.

The rows accumulate rather than being trimmed to the current state, because
"when did this last work" is a question about history, and a table holding only
the latest answer cannot be asked "when did it stop".

Revision ID: 0022_offsite_run
Revises: 0021_offsite_object
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_offsite_run"
down_revision: str | None = "0021_offsite_object"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "offsite_run",
        sa.Column(
            "id", sa.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        # requested → running → succeeded | failed. `requested` exists so the
        # api can ask for a run without performing a 300 MB upload inside a
        # request handler; the worker owns the doing.
        sa.Column("state", sa.Text(), nullable=False, server_default="requested"),
        sa.Column("trigger", sa.Text(), nullable=False, server_default="schedule"),
        sa.Column("requested_by", sa.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("dump_key", sa.Text(), nullable=True),
        sa.Column("dump_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("blobs_uploaded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("blobs_skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bytes_sent", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("failures", sa.JSON(), nullable=True),
    )
    # "When did this last succeed" is the query the Trust screen and the
    # staleness alert both run, and it must not degrade into a table scan as
    # runs accumulate.
    op.create_index(
        "ix_offsite_run_state_started", "offsite_run", ["state", "started_at"]
    )
    # At most one run in flight, enforced by the database rather than by the
    # worker being careful. Two workers, or a restart that leaves a stale
    # `running` row, would otherwise both upload — and with versioning on and
    # no delete permission, a duplicate object version cannot be cleaned up.
    op.create_index(
        "uq_offsite_run_one_at_a_time", "offsite_run", ["state"],
        unique=True, postgresql_where=sa.text("state = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("uq_offsite_run_one_at_a_time", table_name="offsite_run")
    op.drop_index("ix_offsite_run_state_started", table_name="offsite_run")
    op.drop_table("offsite_run")
