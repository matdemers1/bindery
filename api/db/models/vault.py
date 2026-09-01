import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, created_at, uuid_pk


class Vault(Base):
    """One person's vault (ADR-012).

    Per user rather than per library. A library is the boundary between people;
    this is a boundary against the person the library belongs to, which is a
    different question and needs a different answer.
    """

    __tablename__ = "vault"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=False, unique=True
    )
    # The durable copy. Losing this passphrase loses the documents — there is no
    # reset, because a reset would be a way in.
    passphrase_wrapped: Mapped[dict] = mapped_column(sa.JSON, nullable=False)
    # The convenience copy, destroyed after too many failures and rebuilt from
    # the passphrase without re-encrypting anything.
    pin_wrapped: Mapped[dict | None] = mapped_column(sa.JSON)
    pin_failures: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = created_at()
    unlocked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class VaultItem(Base):
    """An encrypted document.

    `object_name` is random and never the plaintext hash. A content address is
    an existence oracle — anyone holding a copy of a file could confirm the
    archive holds it without decrypting anything — which is the leak migration
    0017 closed and T-13.7 found again.
    """

    __tablename__ = "vault_item"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("document.id"), nullable=False, unique=True
    )
    vault_id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("vault.id"), nullable=False
    )
    object_name: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    byte_size: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    sealed_sha256: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
    sealed_meta: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
    original_media_type: Mapped[str | None] = mapped_column(sa.Text)
    # 1 = one AES-GCM message (ADR-012); 2 = independently decryptable chunks
    # (ADR-013). Read by the re-seal that upgrades v1 objects on the next
    # unlock, and by nothing else: `open_object` dispatches on the bytes.
    format_version: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="1"
    )
    page_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    vaulted_at: Mapped[datetime] = created_at()


class VaultPage(Base):
    """Page text, encrypted at rest.

    No tsvector by design. An index of this text on disk is precisely what the
    vault exists to prevent, so searching means decrypting in the api process
    while unlocked — linear, bounded, and leaking nothing when locked.
    """

    __tablename__ = "vault_page"

    id: Mapped[uuid.UUID] = uuid_pk()
    vault_item_id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("vault_item.id"), nullable=False
    )
    page_number: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    sealed_text: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
