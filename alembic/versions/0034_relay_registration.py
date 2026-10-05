"""Push registrations for D3 Constellation (BND-T-23.4).

A device's relay and public key, owned by the native session (the refresh-token row it registered
with) or the OIDC identity that signed it in. Forgotten rather than deleted when either ends
(invariant 3), so the unique slot is partial on the live rows.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0034_relay_registration"
down_revision = "0033_native_session_device"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "relay_registration",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=False
        ),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("refresh_token.id"),
            nullable=True,
        ),
        sa.Column(
            "identity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("oidc_identity.id"),
            nullable=True,
        ),
        sa.Column("device_public_key", sa.LargeBinary(), nullable=False),
        sa.Column("relay_url", sa.Text(), nullable=False),
        sa.Column("registration", sa.Text(), nullable=False),
        sa.Column("send_key_sealed", sa.Text(), nullable=False),
        sa.Column("categories", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("forgotten_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_relay_registration_user_id", "relay_registration", ["user_id"])
    op.create_index(
        "uq_relay_registration_live",
        "relay_registration",
        ["relay_url", "registration"],
        unique=True,
        postgresql_where=sa.text("forgotten_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_relay_registration_live", table_name="relay_registration")
    op.drop_index("ix_relay_registration_user_id", table_name="relay_registration")
    op.drop_table("relay_registration")
