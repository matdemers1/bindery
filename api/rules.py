"""Deterministic rules that override the classifier (REQ-060, REQ-061).

`Correspondent = GEICO → tags {Insurance, Vehicle}`. You wrote it, so it wins.

Rule effects are recorded with `actor_type = 'rule'`, distinct from `ai`, so the
why-panel can say *"this tag came from a rule you wrote"* rather than *"the
model decided"* — a difference that matters when someone is deciding whether to
trust a value.

Lives in `api/` because both processes use it: the pipeline applies rules, and
the API dry-runs them. **A rule cannot be enabled without a dry-run** — the
schema defaults `enabled` to false, and the editor shows what a rule would have
done to documents already in the archive before it is allowed to do it.

Conditions are a small, closed language on purpose. A rule the user cannot
predict the behaviour of is worse than no rule.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Correspondent, Document, DocumentType, KnownForm, Page, Rule, Tag

# field -> how to read it off a document
CONDITION_FIELDS = (
    "correspondent",
    "document_type",
    "known_form",
    "title",
    "text",
    "tag",
)
OPERATORS = ("equals", "contains", "starts_with")


class RuleError(ValueError):
    """A rule is malformed. Surfaced at save time, never at apply time."""


def validate(conditions: dict[str, Any], actions: dict[str, Any]) -> None:
    clauses = conditions.get("all") or []
    if not clauses:
        raise RuleError("a rule needs at least one condition")
    for clause in clauses:
        if clause.get("field") not in CONDITION_FIELDS:
            raise RuleError(
                f"unknown condition field {clause.get('field')!r}; "
                f"expected one of {', '.join(CONDITION_FIELDS)}"
            )
        if clause.get("operator", "equals") not in OPERATORS:
            raise RuleError(f"unknown operator {clause.get('operator')!r}")
        if not str(clause.get("value", "")).strip():
            raise RuleError("a condition needs a value")

    if not (actions.get("add_tags") or actions.get("set_document_type") or
            actions.get("set_sensitivity")):
        raise RuleError("a rule needs at least one action")


@dataclass
class DocumentFacts:
    """Everything a rule may read. Assembled once, so evaluation is pure."""

    document_id: uuid.UUID
    correspondent: str | None = None
    document_type: str | None = None
    known_form: str | None = None
    title: str | None = None
    text: str = ""
    tags: list[str] = field(default_factory=list)


def _matches(clause: dict[str, Any], facts: DocumentFacts) -> bool:
    operator = clause.get("operator", "equals")
    value = str(clause["value"]).strip().casefold()
    field_name = clause["field"]

    if field_name == "tag":
        haystacks = [tag.casefold() for tag in facts.tags]
    else:
        actual = getattr(facts, field_name, None)
        haystacks = [str(actual).casefold()] if actual else []

    if not haystacks:
        return False
    if operator == "equals":
        return any(candidate == value for candidate in haystacks)
    if operator == "starts_with":
        return any(candidate.startswith(value) for candidate in haystacks)
    return any(value in candidate for candidate in haystacks)


def evaluate(rule: Rule, facts: DocumentFacts) -> bool:
    """All clauses must hold. Deliberately no `any` — see the module docstring."""
    return all(_matches(clause, facts) for clause in (rule.conditions.get("all") or []))


async def facts_for(session: AsyncSession, document: Document) -> DocumentFacts:
    async def name_of(model, identifier):
        if identifier is None:
            return None
        row = await session.get(model, identifier)
        return getattr(row, "name", None) or getattr(row, "code", None)

    text = " ".join(
        (
            await session.execute(
                sa.select(Page.text)
                .where(
                    Page.source_file_id == document.source_file_id,
                    Page.page_number >= document.page_start,
                    Page.page_number <= document.page_end,
                )
                .order_by(Page.page_number)
            )
        ).scalars().all()
        or []
    )
    from api.db.models import DocumentTag, live_tag_links

    tags = (
        await session.execute(
            sa.select(Tag.name)
            .join(DocumentTag, DocumentTag.tag_id == Tag.id)
            .where(DocumentTag.document_id == document.id, live_tag_links())
        )
    ).scalars().all()

    return DocumentFacts(
        document_id=document.id,
        correspondent=await name_of(Correspondent, document.correspondent_id),
        document_type=await name_of(DocumentType, document.document_type_id),
        known_form=await name_of(KnownForm, document.known_form_id),
        title=document.title,
        text=text,
        tags=list(tags),
    )


async def enabled_rules(session: AsyncSession, library_id: uuid.UUID) -> list[Rule]:
    return list(
        (
            await session.execute(
                sa.select(Rule)
                .where(Rule.library_id == library_id, Rule.enabled.is_(True))
                .order_by(Rule.priority, Rule.created_at)
            )
        ).scalars().all()
    )
