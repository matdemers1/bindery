"""Linked provider identities, logout events, and the provider session on a refresh row (Phase 20).

Three additions, all of them additive: an account that never signs in through a
provider is unaffected, and an archive whose operator never configures one is
unchanged apart from three empty tables.

`oidc_identity` keys a link on `(issuer, subject)` and on nothing else, with
uniqueness in both directions — one identity per account, one account per
identity. The provider's refresh token rides along encrypted, and buys the one
thing Phase 20 needs it for: re-reading roles when Bindery renews its own
session, so a grant withdrawn upstream lands here without a re-login.

`oidc_logout_event` is the `jti` ledger that makes back-channel logout
idempotent across processes. The SDK's in-memory store says in its own
docstring that it is enough for one process, and a table is what the api and
its successors already share.

`refresh_token.oidc_sid` is what lets a logout name one session rather than
every session a person holds.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as _UUID

revision = "0031_oidc_identities"
down_revision = "0030_read_path_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oidc_identity",
        sa.Column("id", _UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", _UUID(as_uuid=True),
                  sa.ForeignKey("app_user.id"), nullable=False, unique=True),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("preferred_username", sa.Text()),
        sa.Column("refresh_token_enc", sa.Text()),
        sa.Column("origin", sa.Text(), nullable=False, server_default="link"),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("issuer", "subject", name="uq_oidc_identity_issuer_subject"),
    )

    op.create_table(
        "oidc_logout_event",
        sa.Column("jti", sa.Text(), primary_key=True),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("sid", sa.Text()),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("ended_sessions", sa.Integer(), nullable=False, server_default="0"),
    )

    op.add_column("refresh_token", sa.Column("oidc_sid", sa.Text()))
    op.create_index("ix_refresh_token_oidc_sid", "refresh_token", ["oidc_sid"])


def downgrade() -> None:
    op.drop_index("ix_refresh_token_oidc_sid", table_name="refresh_token")
    op.drop_column("refresh_token", "oidc_sid")
    op.drop_table("oidc_logout_event")
    op.drop_table("oidc_identity")
