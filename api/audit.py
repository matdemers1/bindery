"""Append-only audit trail (invariant: every mutation is recorded)."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from api.db.enums import ActorType
from api.db.models import AuditEvent


async def record(
    session: AsyncSession,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    action: str,
    actor_type: ActorType,
    actor_id: uuid.UUID | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    rule_id: uuid.UUID | None = None,
) -> AuditEvent:
    """Add an audit row to the current transaction.

    Deliberately not committed here: the audit row and the mutation it describes
    land together or not at all.
    """
    event = AuditEvent(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        actor_type=actor_type,
        actor_id=actor_id,
        before=before,
        after=after,
        rule_id=rule_id,
    )
    session.add(event)
    return event
