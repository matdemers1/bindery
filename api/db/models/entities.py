import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, created_at, pg_enum, uuid_pk
from api.db.enums import AssetKind, TagSource


class CorrespondentAlias(Base):
    """Another spelling of the same sender.

    *AMERICAN HONDA FINANCE*, *Honda Financial Services* and *Honda Fin Svcs*
    are one company. Resolving them at classification time is what keeps merge a
    rare operation rather than a constant chore.
    """

    __tablename__ = "correspondent_alias"
    __table_args__ = (sa.UniqueConstraint("correspondent_id", "slug"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    correspondent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("correspondent.id"), nullable=False
    )
    alias: Mapped[str] = mapped_column(sa.Text, nullable=False)
    slug: Mapped[str] = mapped_column(sa.Text, nullable=False, index=True)
    created_at: Mapped[datetime] = created_at()


class Asset(Base):
    """A thing documents are *about* — the Honda, the house, the policy.

    This is what makes "show me everything about the Honda" a query rather than
    a search. Many-to-many with documents, because one document can concern
    several assets: an insurance declarations page covers two cars.
    """

    __tablename__ = "asset"
    __table_args__ = (sa.UniqueConstraint("library_id", "slug"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    library_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("library.id"), nullable=False, index=True
    )
    kind: Mapped[AssetKind] = mapped_column(pg_enum(AssetKind, "asset_kind"), nullable=False)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    slug: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # VIN, plate, address, policy number.
    attributes: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    # Stable external id, so linking to VaultDrv later needs no migration.
    external_ref: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = created_at()


class DocumentAsset(Base):
    """Which documents concern which assets. Superseded, never deleted."""

    __tablename__ = "document_asset"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("document.id"), primary_key=True
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("asset.id"), primary_key=True
    )
    source: Mapped[TagSource] = mapped_column(pg_enum(TagSource, "tag_source"), nullable=False)
    created_at: Mapped[datetime] = created_at()
    removed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class SavedSearch(Base):
    """A smart shelf. A packet is the same thing with an export button."""

    __tablename__ = "saved_search"
    __table_args__ = (sa.UniqueConstraint("user_id", "library_id", "name"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    library_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("library.id"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    query: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    is_shelf: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    is_packet: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    created_at: Mapped[datetime] = created_at()


class DuplicatePair(Base):
    """Two documents that look like the same thing.

    **Recorded, never auto-resolved.** Two scans of one deed at different
    qualities are both worth keeping until a human decides otherwise — and
    nothing in this system deletes on its own.
    """

    __tablename__ = "duplicate_pair"
    __table_args__ = (
        # Ordered pair, so (a,b) and (b,a) cannot both exist.
        sa.CheckConstraint("document_a_id < document_b_id", name="ordered"),
        sa.UniqueConstraint("document_a_id", "document_b_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    library_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("library.id"), nullable=False, index=True
    )
    document_a_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("document.id"), nullable=False
    )
    document_b_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("document.id"), nullable=False
    )
    similarity: Mapped[float] = mapped_column(sa.Float, nullable=False)
    dismissed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()
