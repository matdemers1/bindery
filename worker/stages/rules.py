"""Rules: deterministic overrides, then the filing decision.

Rules run *after* the classifier and can overrule it, because you wrote them and
the model did not. Their effects are recorded with `actor_type = 'rule'`, kept
distinct from `ai` all the way down to the document-tag link row (REQ-078), so
the why-panel can attribute a value to the right author.

This stage also makes the final filing call: it re-runs the gate with the rule
signal included, sets `review_state`, and records both. A document that files
itself and a document that lands in review both leave the same audit trail —
"nothing happened" is not a state this system has.
"""

import logging

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api import rules as rules_engine
from api.audit import record
from api.db.enums import ActorType, ReviewState, Sensitivity, TagSource
from api.db.models import Classification, Document, DocumentType, Tag
from api.queue import ClaimedJob
from worker.classify import gate as gate_module
from worker.classify.resolve import apply_tags, slugify

log = logging.getLogger("bindery.worker.rules")


async def _tag_ids_for(session: AsyncSession, names: list[str], library_id) -> list:
    ids = []
    for name in names:
        slug = slugify(name)
        tag = (
            await session.execute(
                sa.select(Tag).where(Tag.library_id == library_id, Tag.slug == slug)
            )
        ).scalar_one_or_none()
        if tag is None:
            tag = Tag(library_id=library_id, name=name.strip(), slug=slug)
            session.add(tag)
            await session.flush()
        ids.append(tag.id)
    return ids


async def apply_rules(
    session: AsyncSession, document: Document, *, dry_run: bool = False
) -> list[dict]:
    """Evaluate every enabled rule against a document.

    Returns what fired and what it would do — the same structure whether or not
    anything is written, which is what makes the dry-run (REQ-061) a true
    preview rather than a separate code path that might disagree.
    """
    facts = await rules_engine.facts_for(session, document)
    applied: list[dict] = []

    for rule in await rules_engine.enabled_rules(session, document.library_id):
        if not rules_engine.evaluate(rule, facts):
            continue

        effects: dict = {"rule_id": str(rule.id), "rule_name": rule.name, "changes": {}}
        actions = rule.actions or {}

        if add_tags := actions.get("add_tags"):
            effects["changes"]["add_tags"] = add_tags
            if not dry_run:
                tag_ids = await _tag_ids_for(session, add_tags, document.library_id)
                await apply_tags(session, document, tag_ids, TagSource.RULE)

        if type_name := actions.get("set_document_type"):
            effects["changes"]["set_document_type"] = type_name
            if not dry_run:
                slug = slugify(type_name)
                existing = (
                    await session.execute(
                        sa.select(DocumentType).where(
                            DocumentType.library_id == document.library_id,
                            DocumentType.slug == slug,
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    existing = DocumentType(
                        library_id=document.library_id, name=type_name, slug=slug
                    )
                    session.add(existing)
                    await session.flush()
                document.document_type_id = existing.id

        if sensitivity := actions.get("set_sensitivity"):
            effects["changes"]["set_sensitivity"] = sensitivity
            if not dry_run:
                document.sensitivity = Sensitivity(sensitivity)

        applied.append(effects)

        if not dry_run:
            await record(
                session,
                entity_type="document",
                entity_id=document.id,
                action="rule_applied",
                actor_type=ActorType.RULE,
                rule_id=rule.id,
                after=effects,
            )

    return applied


async def run_rules(session: AsyncSession, job: ClaimedJob) -> None:
    document = await session.get(Document, job.document_id)
    if document is None or document.superseded_at is not None:
        return

    applied = await apply_rules(session, document)

    latest = (
        await session.execute(
            sa.select(Classification)
            .where(Classification.document_id == document.id)
            .order_by(Classification.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if latest is None:
        # Rules ran without a classification — the provider was unavailable.
        # A rule firing is itself decisive, so a rule-only document can still
        # file; otherwise it waits for the classifier.
        signals = gate_module.GateInputs(rule_fired=bool(applied))
    else:
        signals = gate_module.GateInputs.from_json(latest.structural_signals)
        signals.rule_fired = bool(applied)

    verdict = gate_module.decide(signals)
    before = document.review_state.value
    document.review_state = (
        ReviewState.FILED
        if verdict.decision is gate_module.GateDecision.FILED
        else ReviewState.NEEDS_REVIEW
    )

    if latest is not None:
        latest.structural_signals = signals.to_json()
        latest.gate_decision = verdict.decision.value
        latest.gate_reasons = verdict.reasons

    await record(
        session,
        entity_type="document",
        entity_id=document.id,
        action="file" if document.review_state is ReviewState.FILED else "route_to_review",
        actor_type=ActorType.RULE if applied else ActorType.SYSTEM,
        before={"review_state": before},
        after={
            "review_state": document.review_state.value,
            "gate_score": verdict.score,
            "gate_reasons": verdict.reasons,
        },
    )
    await session.flush()

    log.info(
        "document %s: %s rule(s) fired, gate says %s (%.1f)",
        document.id, len(applied), verdict.decision.value, verdict.score,
    )
