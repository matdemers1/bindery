"""Remove the Batch API tables. The feature was never wired up.

`worker/ai/batch.py` was written in Phase 4 and never called. `batch_submission`
and `batch_request` have been empty in production since they were created.

Deleted rather than kept, and the reasoning is worth recording because the code
was *good* — it keyed results by `custom_id` rather than by position, which is
the one thing that matters about the Batch API and the thing most
implementations get wrong.

Three reasons it goes:

**It has never run.** Not once, against the real API. Code that looks finished
and has never executed is a trap: it accumulates the bugs of every change made
around it while appearing ready. This module had already acquired the exact
failure that cost most of Phase 9 — no `output_config.effort` alongside a model
that thinks by default, and `max_tokens=8000`, which was measured as too small
for a forty-page document. Nobody would have found either until the day someone
switched it on.

**The saving is modest.** Batch is half price. The measured cost of classifying
148 documents with vision was a few dollars; a guest's backlog might be ten to
fifteen, so the saving is a few pounds per household.

**The latency changes the product.** Results take up to 24 hours. The import
screen currently shows files moving through stages in real time, and a backlog
that classifies "sometime tomorrow" is a different, worse experience for the
sake of that saving.

If it comes back, it comes back with a poller, a route, and a screen that says
what is pending — and with `custom_id` keying, which is the part worth keeping
from this attempt.

Revision ID: 0020_drop_unused_batch_tables
Revises: 0019_service_heartbeat
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_drop_unused_batch_tables"
down_revision: str | None = "0019_service_heartbeat"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Both are empty in production — verified before writing this. REQ-090 is
    # about documents and records, and neither table has ever held either.
    op.drop_table("batch_request")
    op.drop_table("batch_submission")


def downgrade() -> None:
    op.create_table(
        "batch_submission",
        sa.Column(
            "id", sa.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("session_id", sa.UUID(as_uuid=True), sa.ForeignKey("import_session.id")),
        sa.Column("provider_batch_id", sa.Text(), nullable=False, unique=True),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="submitted"),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_table(
        "batch_request",
        sa.Column(
            "id", sa.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "submission_id", sa.UUID(as_uuid=True),
            sa.ForeignKey("batch_submission.id"), nullable=False,
        ),
        sa.Column("custom_id", sa.Text(), nullable=False),
        sa.Column("document_id", sa.UUID(as_uuid=True), sa.ForeignKey("document.id")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("submission_id", "custom_id"),
    )
