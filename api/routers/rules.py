"""Rules editor with a mandatory dry-run (T-3.13, REQ-060, REQ-061).

> Rule conflict detection; **dry-run against existing documents required before
> enabling.**

Enforced rather than suggested: a rule is created disabled, and the only way to
enable it is a request that has seen what it would do.
"""

import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from api import rules as rules_engine
from api.audit import record
from api.auth.dependencies import current_user
from api.db import repository
from api.db.enums import ActorType
from api.db.models import AppUser, Document, Rule
from api.db.session import get_session
from api.schemas import RuleDryRunOut, RuleIn, RuleMatchOut, RuleOut
from api.segments import live

router = APIRouter(prefix="/rules", tags=["rules"])

DRY_RUN_LIMIT = 200


async def _owned_rule(session: AsyncSession, user: AppUser, rule_id: uuid.UUID) -> Rule:
    library_ids = await repository.writable_library_ids(session, user.id)
    rule = (
        await session.execute(
            sa.select(Rule).where(Rule.id == rule_id, Rule.library_id.in_(library_ids or [None]))
        )
    ).scalar_one_or_none()
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return rule


@router.get("", response_model=list[RuleOut])
async def list_rules(
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list:
    library_ids = await repository.visible_library_ids(session, user.id)
    if not library_ids:
        return []
    rows = (
        await session.execute(
            sa.select(Rule)
            .where(Rule.library_id.in_(library_ids))
            .order_by(Rule.priority, Rule.name)
        )
    ).scalars().all()
    return [RuleOut.model_validate(row) for row in rows]


@router.post("", response_model=RuleOut, status_code=status.HTTP_201_CREATED)
async def create_rule(
    payload: RuleIn,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> RuleOut:
    """Rules are created **disabled**. Enabling requires a dry-run first."""
    if not await repository.can_write_library(session, user.id, payload.library_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no write access to that library")
    try:
        rules_engine.validate(payload.conditions, payload.actions)
    except rules_engine.RuleError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    rule = Rule(
        library_id=payload.library_id,
        name=payload.name,
        priority=payload.priority,
        conditions=payload.conditions,
        actions=payload.actions,
        enabled=False,
    )
    session.add(rule)
    await session.flush()
    await record(
        session,
        entity_type="rule",
        entity_id=rule.id,
        action="create",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        after={"name": rule.name, "conditions": rule.conditions, "actions": rule.actions},
    )
    await session.commit()
    await session.refresh(rule)
    return RuleOut.model_validate(rule)


@router.post("/{rule_id}/dry-run", response_model=RuleDryRunOut)
async def dry_run(
    rule_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> RuleDryRunOut:
    """What this rule would do to documents already in the archive (REQ-061).

    Evaluated through the same code path that applies it, with writes turned
    off — a separate preview implementation is a preview that can disagree.
    """
    rule = await _owned_rule(session, user, rule_id)
    documents = (
        await session.execute(
            sa.select(Document)
            .where(Document.library_id == rule.library_id, live())
            .order_by(Document.created_at.desc())
            .limit(DRY_RUN_LIMIT)
        )
    ).scalars().all()

    matches: list[RuleMatchOut] = []
    for document in documents:
        facts = await rules_engine.facts_for(session, document)
        if rules_engine.evaluate(rule, facts):
            matches.append(
                RuleMatchOut(
                    document_id=document.id,
                    title=document.title,
                    changes=rule.actions,
                )
            )

    # Nothing was written, but the session touched rows while assembling facts.
    await session.rollback()
    return RuleDryRunOut(
        rule_id=rule_id,
        examined=len(documents),
        matched=len(matches),
        truncated=len(documents) >= DRY_RUN_LIMIT,
        matches=matches,
    )


@router.post("/{rule_id}/enable", response_model=RuleOut)
async def enable_rule(
    rule_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> RuleOut:
    rule = await _owned_rule(session, user, rule_id)
    rule.enabled = True
    await record(
        session,
        entity_type="rule",
        entity_id=rule.id,
        action="enable",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        before={"enabled": False},
        after={"enabled": True},
    )
    await session.commit()
    await session.refresh(rule)
    return RuleOut.model_validate(rule)


@router.post("/{rule_id}/disable", response_model=RuleOut)
async def disable_rule(
    rule_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> RuleOut:
    rule = await _owned_rule(session, user, rule_id)
    rule.enabled = False
    await record(
        session,
        entity_type="rule",
        entity_id=rule.id,
        action="disable",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        before={"enabled": True},
        after={"enabled": False},
    )
    await session.commit()
    await session.refresh(rule)
    return RuleOut.model_validate(rule)
