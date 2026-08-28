"""Backfill the backlog flag onto documents that already exist.

`mark_backlog` ran from the import endpoint immediately after ingest — before
normalize, paging and segmentation had produced any documents to mark. It
flagged whatever happened to exist from an earlier slice: in a real 500-file
import, 15 of 396. The other 381 went into the daily review queue, which is the
precise pile the flag exists to prevent (R-03, REQ-083).

Documents now inherit the flag from their source file at creation. This fixes
the ones already in the database, which is the difference between a review
queue of three hundred and a review queue of a dozen.

Revision ID: 0012_backfill_backlog_flag
Revises: 0011_event_log
Create Date: 2026-08-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012_backfill_backlog_flag"
down_revision: str | Sequence[str] | None = "0011_event_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # How a file arrived is recorded on the file itself, so the flag can be
    # derived rather than guessed.
    op.execute(
        """
        UPDATE document AS d
           SET is_backlog = true
          FROM source_file AS f
         WHERE f.id = d.source_file_id
           AND f.ingest_source = 'bulk_import'
           AND d.is_backlog = false
        """
    )


def downgrade() -> None:
    # Deliberately not reversed: un-flagging would push these documents back
    # into the review queue, which is the bug rather than the previous state.
    pass
