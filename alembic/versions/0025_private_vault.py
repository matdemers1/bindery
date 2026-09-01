"""The private vault (Phase 16, ADR-012).

Three tables and one column.

`vault` is per user (one row each): the data key wrapped by a passphrase, the
same key wrapped by a PIN, and the failure counter that destroys the PIN copy
without touching the passphrase one.

`vault_item` is the encrypted document: a **random** identifier rather than a
content address, because a content address is an existence oracle — anyone
holding a copy of a file could confirm the archive holds it without decrypting
anything. That is the leak migration 0017 closed and T-13.7 found again.

`vault_page` holds page text encrypted at rest. It exists because leaving a
vaulted document's text in `page.text_tsv` leaks it *while locked*, through
snippets, hit counts, facets and the palette — none of which are the document
endpoint anyone would think to guard.

`document.vaulted_by` is the lock: the repository layer refuses to return a row
carrying it unless the request has an unlocked session for that user.

Revision ID: 0025_private_vault
Revises: 0024_job_declined_state
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_private_vault"
down_revision: str | None = "0024_job_declined_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "vault",
        sa.Column(
            "id", sa.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # Per user, not per library. A library is the boundary between people
        # (ADR-005); this is a boundary against the person the library belongs
        # to, which is a different question.
        sa.Column(
            "user_id", sa.UUID(as_uuid=True),
            sa.ForeignKey("app_user.id", ondelete="RESTRICT"),
            nullable=False, unique=True,
        ),
        # The durable copy. Losing this passphrase loses the documents.
        sa.Column("passphrase_wrapped", sa.JSON(), nullable=False),
        # The convenience copy: destroyed after too many failures and rebuilt
        # from the passphrase, without re-encrypting a single file.
        sa.Column("pin_wrapped", sa.JSON(), nullable=True),
        sa.Column("pin_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("unlocked_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "vault_item",
        sa.Column(
            "id", sa.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "document_id", sa.UUID(as_uuid=True),
            sa.ForeignKey("document.id", ondelete="RESTRICT"),
            nullable=False, unique=True,
        ),
        sa.Column(
            "vault_id", sa.UUID(as_uuid=True),
            sa.ForeignKey("vault.id", ondelete="RESTRICT"), nullable=False,
        ),
        # Random, never the plaintext hash. See the note above.
        sa.Column("object_name", sa.Text(), nullable=False, unique=True),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        # The original SHA-256, encrypted. Kept so integrity is still checkable
        # after unlocking, and useless to anyone who is not.
        sa.Column("sealed_sha256", sa.LargeBinary(), nullable=False),
        # Title, dates and original filename, encrypted together.
        sa.Column("sealed_meta", sa.LargeBinary(), nullable=False),
        sa.Column("original_media_type", sa.Text(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "vaulted_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_vault_item_vault", "vault_item", ["vault_id"])

    op.create_table(
        "vault_page",
        sa.Column(
            "id", sa.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "vault_item_id", sa.UUID(as_uuid=True),
            sa.ForeignKey("vault_item.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("page_number", sa.Integer(), nullable=False),
        # No tsvector, deliberately. A searchable index of this text on disk is
        # the thing the vault exists to prevent; searching means decrypting in
        # the api process while unlocked (ADR-012).
        sa.Column("sealed_text", sa.LargeBinary(), nullable=False),
        sa.UniqueConstraint("vault_item_id", "page_number"),
    )
    op.create_index("ix_vault_page_item", "vault_page", ["vault_item_id"])

    op.add_column(
        "document",
        sa.Column(
            "vaulted_by", sa.UUID(as_uuid=True),
            sa.ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True,
        ),
    )
    # Every scoped query filters on this, so it must not be a sequential scan.
    op.create_index("ix_document_vaulted_by", "document", ["vaulted_by"])


def downgrade() -> None:
    op.drop_index("ix_document_vaulted_by", table_name="document")
    op.drop_column("document", "vaulted_by")
    op.drop_table("vault_page")
    op.drop_index("ix_vault_item_vault", table_name="vault_item")
    op.drop_table("vault_item")
    op.drop_table("vault")
