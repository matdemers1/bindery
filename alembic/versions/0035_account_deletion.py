"""An account can be deleted from D3 Constellation (BND-T-23.3, BND-ADR-015).

Two nullable columns, so this is expand-only. `delete_after` is set when a person asks for their
account to be deleted and cleared if an administrator restores it during the grace period;
`deleted_at` is set when the purge has run and the row is a tombstone. Every row before this one
is neither.
"""

import sqlalchemy as sa
from alembic import op

revision = "0035_account_deletion"
down_revision = "0034_relay_registration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_user", sa.Column("delete_after", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "app_user", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("app_user", "deleted_at")
    op.drop_column("app_user", "delete_after")
