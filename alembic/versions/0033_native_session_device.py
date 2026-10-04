"""Native sessions name their device (BND-T-22.2, CON-REQ-023).

A session D3 Constellation signs in is listed in the sessions screen beside the browser ones, and
the only thing that tells a person which row is their phone is the name the phone gave. Both
columns are nullable: every session before this one came from a browser, which names nothing.
"""

import sqlalchemy as sa
from alembic import op

revision = "0033_native_session_device"
down_revision = "0032_taxonomy_library_required"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("refresh_token", sa.Column("device_name", sa.Text(), nullable=True))
    op.add_column("refresh_token", sa.Column("device_platform", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("refresh_token", "device_platform")
    op.drop_column("refresh_token", "device_name")
