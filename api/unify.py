"""Proposing which correspondents are the same organisation (T-8.17).

An archive built from twenty years of documents ends up with *26th Weapon
School*, *26th Weapons School*, *26th Weapons Squadron* and *26th Weapons
Squadron (USAF Weapons School)* — four folders for one unit, because that is
how four different letterheads spelled it.

Trigram similarity already finds pairs that look alike, and it is the right
tool for a typo. It cannot tell you that a squadron and its school are the same
organisation, or that they are deliberately different ones — that is knowledge
about the world, not about the strings.

So this asks a model, and then does nothing with the answer until a person
agrees. Three properties make that safe:

**Only names are sent.** Never document text. The request is a list of labels,
which is cheap and means nothing about the contents of the archive leaves it.

**Every proposal is a preview.** The result is a suggestion with a reason
attached; applying it runs the ordinary merge path, which is audited and
undoable as a single operation.

**Aliases, not deletions.** A merge keeps the old name as an alias pointing at
the survivor, so a future document that spells it the old way still lands in
the right place — which is what stops the same four folders growing back.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Correspondent, Document
from api.segments import live

log = logging.getLogger("bindery.unify")

# Beyond this the prompt stops being a list and starts being a corpus. A
# household archive has tens of correspondents, not thousands.
MAX_NAMES = 400

PROMPT = """You are tidying the list of organisations a personal document archive files under.

Below is every name currently in use, with how many documents each has. Some of
them refer to the same real organisation spelled differently across letterheads,
abbreviations, or a rename. Group only those.

Rules:
- Group names ONLY when you are confident they are the same organisation. A
  parent and a subsidiary are different. Two branches of one bank are the same.
- A unit and its school, or a squadron and its wing, are DIFFERENT organisations
  unless the names make clear they are one and the same.
- Prefer the fullest, most formal spelling as the canonical name, unless a
  shorter one is obviously the everyday name.
- Leave anything you are unsure about out entirely. A missed merge costs
  nothing; a wrong one puts two organisations' documents in one folder.
- Give a one-line reason for each group, in plain language.

Return JSON only, in exactly this shape:
{"groups": [{"canonical": "<name>", "members": ["<name>", "<name>"], "reason": "<why>"}]}
"""


@dataclass
class ProposedGroup:
    canonical: str
    canonical_id: uuid.UUID | None
    members: list[dict] = field(default_factory=list)
    reason: str = ""

    @property
    def document_count(self) -> int:
        return sum(member.get("documents", 0) for member in self.members)


@dataclass
class UnifyProposal:
    groups: list[ProposedGroup]
    considered: int
    model: str | None = None
    unavailable_reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "considered": self.considered,
            "model": self.model,
            "unavailable_reason": self.unavailable_reason,
            "groups": [
                {
                    "canonical": g.canonical,
                    "canonical_id": str(g.canonical_id) if g.canonical_id else None,
                    "reason": g.reason,
                    "document_count": g.document_count,
                    "members": g.members,
                }
                for g in self.groups
            ],
        }


async def _current_names(
    session: AsyncSession, library_ids: list[uuid.UUID]
) -> list[dict]:
    """Live correspondents and how much each is actually carrying."""
    rows = (
        await session.execute(
            sa.select(
                Correspondent.id,
                Correspondent.name,
                sa.func.count(Document.id).label("documents"),
            )
            .outerjoin(
                Document,
                sa.and_(Document.correspondent_id == Correspondent.id, live()),
            )
            .where(
                Correspondent.library_id.in_(library_ids),
                # A name already merged away is not a candidate for merging again.
                Correspondent.merged_into_id.is_(None),
            )
            .group_by(Correspondent.id, Correspondent.name)
            .order_by(Correspondent.name)
        )
    ).all()
    return [
        {"id": str(row.id), "name": row.name, "documents": row.documents}
        for row in rows
    ]


async def propose(
    session: AsyncSession, library_ids: list[uuid.UUID], answerer
) -> UnifyProposal:
    """Ask which of these names are the same organisation. Changes nothing."""
    names = await _current_names(session, library_ids)
    if len(names) < 2:
        return UnifyProposal(groups=[], considered=len(names))
    if answerer is None or not answerer.available():
        return UnifyProposal(
            groups=[],
            considered=len(names),
            unavailable_reason=(
                "No API key is configured, so nothing can be proposed. The "
                "near-duplicate list on this page is found without one."
            ),
        )

    listing = "\n".join(
        f"- {entry['name']}  ({entry['documents']} documents)" for entry in names[:MAX_NAMES]
    )
    try:
        raw = await answerer.complete(PROMPT + "\n\n" + listing)
    except Exception as error:
        log.error("could not propose unifications: %s", error)
        return UnifyProposal(
            groups=[], considered=len(names),
            unavailable_reason=f"Could not reach the model ({error}).",
        )

    return _parse(raw, names, getattr(answerer, "model", None))


def _parse(raw: str, names: list[dict], model: str | None) -> UnifyProposal:
    """Turn the response into proposals, resolving every name back to a record.

    A name the model returns that is not in the archive is dropped rather than
    created: the task was to group what exists, and inventing a folder is the
    opposite of tidying.
    """
    text = raw.strip()
    if "```" in text:
        text = text.split("```")[1].removeprefix("json").strip()

    try:
        payload = json.loads(text)
    except ValueError:
        log.warning("unification response was not JSON")
        return UnifyProposal(
            groups=[], considered=len(names), model=model,
            unavailable_reason="The model's answer could not be read as a proposal.",
        )

    by_name = {entry["name"].strip().lower(): entry for entry in names}
    groups: list[ProposedGroup] = []

    for group in payload.get("groups", []):
        members = []
        for member_name in group.get("members", []):
            entry = by_name.get(str(member_name).strip().lower())
            if entry is not None:
                members.append(entry)

        # A group of one merges nothing.
        if len(members) < 2:
            continue

        canonical_name = str(group.get("canonical", "")).strip()
        canonical = by_name.get(canonical_name.lower())
        if canonical is None:
            # The model named a survivor that does not exist. Rather than
            # invent it, keep the member carrying the most documents — the
            # merge is still correct, only the label is less pretty.
            canonical = max(members, key=lambda m: m["documents"])
            canonical_name = canonical["name"]

        groups.append(
            ProposedGroup(
                canonical=canonical_name,
                canonical_id=uuid.UUID(canonical["id"]),
                members=members,
                reason=str(group.get("reason", "")).strip(),
            )
        )

    log.info("proposed %s grouping(s) across %s names", len(groups), len(names))
    return UnifyProposal(groups=groups, considered=len(names), model=model)
