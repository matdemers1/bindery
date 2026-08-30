"""Login attempts and account lockout, before the outer gate comes off.

Cloudflare Access has been the thing standing between the internet and this
application since Phase 0. ADR-008 removes it, which makes Bindery's own login
page the front door — so the controls that Access was incidentally providing
have to exist first. This is the storage half of two of them (REQ-133, REQ-134).

`login_attempt` keeps every attempt, including ones against addresses that do
not exist: a spray against invented names is invisible if you only record
attempts that matched a user.

Revision ID: 0015_login_hardening
Revises: 0014_document_type_merge
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_login_hardening"
down_revision: str | None = "0014_document_type_merge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "app_user",
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "login_attempt",
        sa.Column(
            "id", sa.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("ip", sa.Text(), nullable=True),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # The throttle asks two questions on every attempt — "how many failures from
    # this address lately" and "how many against this account" — and both run
    # before the password is checked, so they are on the hot path of every login.
    op.create_index(
        "ix_login_attempt_ip_recent", "login_attempt", ["ip", "created_at"]
    )
    op.create_index(
        "ix_login_attempt_email_recent", "login_attempt", ["email", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_login_attempt_email_recent", table_name="login_attempt")
    op.drop_index("ix_login_attempt_ip_recent", table_name="login_attempt")
    op.drop_table("login_attempt")
    op.drop_column("app_user", "locked_until")
