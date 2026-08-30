"""Accounts: invitations, TOTP, reset codes, quotas, suspension (Phase 10b/10c).

Everything here exists so an account can be handed to someone who is not the
operator. Two shapes are worth noting because they encode decisions rather than
storage:

`invitation` and `password_reset_code` store **hashes**, not values. There is no
email service (ADR-008), so both are handed over out of band — a link sent by
text, a code read out loud — and neither can be recovered from the database
afterwards. Shown once, like an API token.

`app_user.is_admin` administers *accounts*, never documents. There is no admin
branch in `visible_library_ids` and there is no endpoint that sets another
user's password. See ADR-009.

Revision ID: 0016_accounts
Revises: 0015_login_hardening
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_accounts"
down_revision: str | None = "0015_login_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = sa.UUID(as_uuid=True)


def _id() -> sa.Column:
    return sa.Column(
        "id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")
    )


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False,
        server_default=sa.text("now()"),
    )


def upgrade() -> None:
    op.add_column(
        "app_user",
        sa.Column(
            "is_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "app_user", sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("app_user", sa.Column("suspended_by_id", _UUID, nullable=True))
    op.add_column(
        "app_user", sa.Column("storage_quota_bytes", sa.BigInteger(), nullable=True)
    )
    op.add_column("app_user", sa.Column("totp_secret", sa.Text(), nullable=True))
    op.add_column(
        "app_user",
        sa.Column("totp_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Without this a TOTP code stays valid for the whole +/-1-step window, up to
    # 90 seconds — ample time to reuse one that was captured.
    op.add_column("app_user", sa.Column("totp_last_step", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_app_user_suspended_by_id_app_user",
        "app_user", "app_user", ["suspended_by_id"], ["id"],
    )

    # The first account becomes the administrator. Bindery has been a
    # single-operator system until now, so there is exactly one account and it
    # is the person who set the host up; leaving every account non-admin would
    # lock the panel against its own owner.
    op.execute(
        "update app_user set is_admin = true "
        "where id = (select id from app_user order by created_at limit 1)"
    )

    op.create_table(
        "invitation",
        _id(),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("library_name", sa.Text(), nullable=False),
        sa.Column("storage_quota_bytes", sa.BigInteger(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_by_id", _UUID, sa.ForeignKey("app_user.id"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_user_id", _UUID, sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
    )
    op.create_index("ix_invitation_email", "invitation", ["email"])

    op.create_table(
        "password_reset_code",
        _id(),
        sa.Column("user_id", _UUID, sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("issued_by_id", _UUID, sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
    )
    op.create_index("ix_password_reset_code_user_id", "password_reset_code", ["user_id"])

    op.create_table(
        "recovery_code",
        _id(),
        sa.Column("user_id", _UUID, sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        # Retired when a new set is issued. Separate from `used_at` so "which
        # codes did I spend" survives a regeneration (REQ-090).
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
    )
    op.create_index("ix_recovery_code_user_id", "recovery_code", ["user_id"])


def downgrade() -> None:
    op.drop_table("recovery_code")
    op.drop_table("password_reset_code")
    op.drop_index("ix_invitation_email", table_name="invitation")
    op.drop_table("invitation")
    op.drop_constraint(
        "fk_app_user_suspended_by_id_app_user", "app_user", type_="foreignkey"
    )
    for column in (
        "totp_last_step", "totp_confirmed_at", "totp_secret", "storage_quota_bytes",
        "suspended_by_id", "suspended_at", "is_admin",
    ):
        op.drop_column("app_user", column)
