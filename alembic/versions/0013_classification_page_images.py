"""Record how many pictures a classification actually looked at.

The photo wall asks "what has nothing been said about" and answered it, until
now, with "what did OCR fail to read" — a fair proxy right up to the moment a
vision pass fixed 146 of them. OCR still reads nothing off a photograph, so all
148 stayed on the list, and the wall went on offering to describe them again at
real cost per press.

"Was there text" and "has anything looked at this" are different questions. This
column is the second one: a classification that carried page images saw the
document, whatever OCR made of it.

Existing rows are backfilled from the stored request artifact, which is the
exact request and therefore says plainly whether a picture was in it. Anything
whose artifact is missing gets 0: that costs one re-run, where guessing 1 would
wrongly claim a document had been looked at.

Revision ID: 0013_classification_page_images
Revises: 0012_backfill_backlog_flag
Create Date: 2026-08-29
"""

import os
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0013_classification_page_images"
down_revision: str | None = "0012_backfill_backlog_flag"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The stored request is the exact request — `raw_request["user"]`, image blocks
# and all — so it can be asked, years later, what was actually sent. Which makes
# it the one honest source for the backfill: not "when did the deploy happen",
# but "does this request contain a picture".
_IMAGE_MARKER = b'"type": "image"'
_MAX_SCAN_BYTES = 64 * 1024 * 1024


def _carried_an_image(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            scanned = 0
            tail = b""
            while chunk := handle.read(1 << 20):
                scanned += len(chunk)
                if _IMAGE_MARKER in tail + chunk:
                    return True
                # Keep enough overlap that a marker split across the boundary
                # is still found.
                tail = chunk[-len(_IMAGE_MARKER):]
                if scanned > _MAX_SCAN_BYTES:
                    return False
    except OSError:
        # A missing artifact means the answer is unknowable, and 0 is the
        # answer that costs a re-run rather than wrongly claiming one happened.
        return False
    return False


def upgrade() -> None:
    op.add_column(
        "classification",
        sa.Column(
            "page_images_sent",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    data_root = Path(os.environ.get("DATA_ROOT", "/data"))
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "select id, request_artifact_path from classification "
            "where request_artifact_path is not null"
        )
    ).fetchall()

    seen = [row.id for row in rows if _carried_an_image(data_root / row.request_artifact_path)]
    for start in range(0, len(seen), 500):
        connection.execute(
            sa.text(
                "update classification set page_images_sent = 1 where id = any(:ids)"
            ),
            {"ids": seen[start : start + 500]},
        )


def downgrade() -> None:
    op.drop_column("classification", "page_images_sent")
