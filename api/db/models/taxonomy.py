import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, created_at, uuid_pk


class DocumentType(Base):
    """Soft, model-suggested classification.

    Unlike `known_form`, which is deterministic and a fact, a document type is an
    opinion — "utility bill", "insurance declarations". Reuse is enforced the
    same way: the model returns an existing id or proposes a new name, never a
    free-text string that silently becomes a near-duplicate.
    """

    __tablename__ = "document_type"
    __table_args__ = (
        sa.UniqueConstraint("library_id", "slug", postgresql_nulls_not_distinct=True),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    library_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("library.id"), index=True
    )
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    slug: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime] = created_at()


class Correspondent(Base):
    """Who a document is from.

    Aliases and merge arrive in Phase 5 (T-5.1, T-5.2); the entity exists now
    because classification has to be able to resolve one.
    """

    __tablename__ = "correspondent"
    __table_args__ = (
        sa.UniqueConstraint("library_id", "slug", postgresql_nulls_not_distinct=True),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    library_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("library.id"), index=True
    )
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    slug: Mapped[str] = mapped_column(sa.Text, nullable=False)
    kind: Mapped[str | None] = mapped_column(sa.Text)
    notes: Mapped[str | None] = mapped_column(sa.Text)
    # Set when this record was merged away. Tombstoned rather than deleted, so
    # undo is a flag flip and historical references still resolve.
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("correspondent.id")
    )
    merged_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at()


class Rule(Base):
    """A deterministic override the user wrote.

    Rules run *after* classification and can overrule it. Their effects are
    recorded with `actor_type = 'rule'`, distinct from `ai`, so the why-panel can
    say "this tag came from a rule you wrote" rather than "the model decided"
    (REQ-060).
    """

    __tablename__ = "rule"

    id: Mapped[uuid.UUID] = uuid_pk()
    library_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("library.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    priority: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="100")
    conditions: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    actions: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = created_at()
