"""What has actually reached the bucket (T-13.4, REQ-161).

Blobs are content-addressed, immutable and append-only, so replication is
incremental forever: 496 objects uploaded once, then only what is new. The
question every run has to answer cheaply is "which of these is already there",
and asking S3 once per blob would be 496 round trips to learn almost nothing.

So this is a ledger. It is an **optimisation, not the truth** — it cannot know
the bucket was emptied by hand, a lifecycle rule was misconfigured, or an
object was removed from the console. The bucket is the truth, and
`verified_at` records the last time the two were actually compared.

Versioning is on and the credential cannot delete, so a needless re-upload is
not merely wasted transfer: it creates an object version that nothing in the
system is able to clean up.

Revision ID: 0021_offsite_object
Revises: 0020_drop_unused_batch_tables
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_offsite_object"
down_revision: str | None = "0020_drop_unused_batch_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "offsite_object",
        sa.Column(
            "id", sa.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # The S3 key, which is the identity. Unique so a concurrent or repeated
        # run cannot record the same object twice — the upload is idempotent
        # and the ledger has to be too.
        sa.Column("object_key", sa.Text(), nullable=False, unique=True),
        # Null for objects that are not content-addressed (dumps, manifests).
        # For blobs it is the same hash the archive already keys them by, and
        # it is what S3 verifies the transfer against.
        sa.Column("sha256", sa.Text(), nullable=True),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column(
            "uploaded_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        # When a ListBucket last confirmed this object is really there. Null
        # means "recorded but never independently checked", which is a
        # different and weaker claim than "verified present".
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The sync's hot path: "which of these hashes do I already have".
    op.create_index("ix_offsite_object_sha256", "offsite_object", ["sha256"])


def downgrade() -> None:
    op.drop_index("ix_offsite_object_sha256", table_name="offsite_object")
    op.drop_table("offsite_object")
