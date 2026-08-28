import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, created_at, pg_enum, uuid_pk
from api.db.enums import ActorType


class AuditEvent(Base):
    """Append-only record of every mutation, human or machine.

    `before`/`after` are what make undo mechanical rather than bespoke, and this
    table is a load-bearing part of Auditable Automation rather than a log.
    Nothing updates or removes rows here.
    """

    __tablename__ = "audit_event"
    __table_args__ = (sa.Index("ix_audit_event_entity", "entity_type", "entity_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    # Monotonic insertion order. `created_at` cannot serve: it defaults to
    # now(), which is the transaction's start time, so two events written in one
    # transaction carry the same timestamp.
    sequence: Mapped[int] = mapped_column(
        sa.BigInteger, sa.Identity(always=False), nullable=False, unique=True
    )
    entity_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(sa.Text, nullable=False)
    actor_type: Mapped[ActorType] = mapped_column(
        pg_enum(ActorType, "actor_type"), nullable=False
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # FK to `rule` is added in Phase 3, when that table exists.
    rule_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at()
