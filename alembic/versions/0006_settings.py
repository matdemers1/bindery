"""Runtime settings, editable from the UI.

Configuration that a person should be able to change without editing a compose
file and restarting containers — the API key above all, because "add your key"
is the first thing anyone does and shelling into a NAS to do it is a bad answer.

Secrets are stored encrypted rather than plain. The encryption key is derived
from JWT_SECRET, which lives in the environment, so a leaked database dump does
not hand over the API key. It is not protection against someone who already has
the host; it is protection against a backup landing somewhere it shouldn't.

Revision ID: 0006_settings
Revises: 0005_extracted_fields
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_settings"
down_revision: str | None = "0005_extracted_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "setting",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text()),
        # True when `value` is Fernet-encrypted and must never be returned to a
        # client — only ever "configured: true" plus a masked hint.
        sa.Column("is_secret", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_by", sa.dialects.postgresql.UUID(as_uuid=True)),
        sa.ForeignKeyConstraint(["updated_by"], ["app_user.id"],
                                name="fk_setting_updated_by_app_user"),
    )


def downgrade() -> None:
    op.drop_table("setting")
