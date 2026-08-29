"""Proposing which taxonomy entries are the same thing (T-8.17, T-9.2).

An archive built from twenty years of documents ends up with *26th Weapon
School*, *26th Weapons School*, *26th Weapons Squadron* and *26th Weapons
Squadron (USAF Weapons School)* — four folders for one unit, because that is
how four different letterheads spelled it.

Trigram similarity already finds pairs that look alike, and it is the right
tool for a typo. It cannot tell you that a squadron and its school are the same
organisation, or that they are deliberately different ones — that is knowledge
about the world, not about the strings.

The same problem arrived at the document types and tags, harder. When R-08
fired, 166 of 277 types and 476 of 699 tags were used exactly once — `Unit
Patch`, `Unit Patch Image`, `Unit Emblem` and `Insignia` all describing the same
kind of thing. T-9.1 fixed the cause; this collapses what the cause produced.

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

from api.db.models import (
    Correspondent,
    Document,
    DocumentTag,
    DocumentType,
    Tag,
    live_tag_links,
)
from api.segments import live

log = logging.getLogger("bindery.unify")

# Beyond this the prompt stops being a list and starts being a corpus. A
# household archive has tens of correspondents, not thousands.
MAX_NAMES = 400

_SHARED_RULES = """
Rules:
- Group entries ONLY when you are confident they mean the same thing. Leave
  anything you are unsure about out entirely: a missed merge costs nothing, and
  a wrong one puts two different things in one folder.
- Prefer the clearest, most conventional name as the canonical one.
- Give a one-line reason for each group, in plain language.

Return JSON only, in exactly this shape:
{"groups": [{"canonical": "<name>", "members": ["<name>", "<name>"], "reason": "<why>"}]}
"""

CORRESPONDENT_PROMPT = """You are tidying the list of organisations that a
personal document archive files under.

Below is every name currently in use, with how many documents each has. Some of
them refer to the same real organisation spelled differently across letterheads,
abbreviations, or a rename. Group only those.

Specific to organisations:
- A parent and a subsidiary are DIFFERENT. Two branches of one bank are the same.
- A unit and its school, or a squadron and its wing, are DIFFERENT organisations
  unless the names make clear they are one and the same.
- Prefer the fullest, most formal spelling, unless a shorter one is obviously
  the everyday name.
""" + _SHARED_RULES

DOCUMENT_TYPE_PROMPT = """You are tidying the list of document *kinds* that a
personal archive files under.

Below is every type currently in use, with how many documents each has. Many
were invented one at a time while classifying and describe the same kind of
thing in different words — "Unit Patch", "Unit Patch Image", "Unit Emblem" and
"Insignia" are one kind of document, not four.

Specific to document types:
- Group by what the document *is*, not by what it is about. "Bank Statement" and
  "Credit Card Statement" are different kinds; "Bank Statement" and "Monthly
  Bank Statement" are the same kind.
- A type describing the *subject* ("Aircraft Diagram") and one describing the
  *form* ("Diagram") are the same kind only if the archive would never want to
  tell them apart. When in doubt, leave them separate.
- Types that describe a failure to read — "Blank or Unreadable Scan",
  "Illegible Scan" — should be grouped together under the clearest of them.
""" + _SHARED_RULES

TAG_PROMPT = """You are tidying the tag vocabulary of a personal document archive.

Below is every tag currently in use, with how many documents each carries. Tags
accumulate faster than any other kind of label, and most of the duplication is
plural-versus-singular, an abbreviation, or two phrasings of one idea.

Specific to tags:
- Singular and plural of the same word are the same tag.
- An abbreviation and its expansion are the same tag.
- A broader tag and a narrower one are DIFFERENT — "medical" and "dental" both
  earn their place. Do not collapse a hierarchy into its root.
""" + _SHARED_RULES


@dataclass(frozen=True)
class Kind:
    """One taxonomy the unify pass can work over.

    Three near-identical shapes with three genuinely different prompts. The
    prompt is the part that matters: the rule that separates a squadron from its
    school has nothing in common with the rule that separates `medical` from
    `dental`, and a single generic instruction gets both wrong.
    """

    key: str
    label: str
    model: type
    prompt: str
    merge: str  # the name of the function in api.entities that applies it


KINDS: dict[str, Kind] = {
    "correspondent": Kind(
        key="correspondent", label="folders", model=Correspondent,
        prompt=CORRESPONDENT_PROMPT, merge="merge_correspondents",
    ),
    "document_type": Kind(
        key="document_type", label="document types", model=DocumentType,
        prompt=DOCUMENT_TYPE_PROMPT, merge="merge_document_types",
    ),
    "tag": Kind(
        key="tag", label="tags", model=Tag, prompt=TAG_PROMPT, merge="merge_tags",
    ),
}


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
    kind: str = "correspondent"

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
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
    session: AsyncSession, kind: Kind, library_ids: list[uuid.UUID]
) -> list[dict]:
    """Live entries of this kind, and how much each is actually carrying.

    Usage matters to the prompt, not just as trivia: the model is told to prefer
    the clearest name, and a name carrying sixty documents is usually the one
    the archive has settled on.
    """
    model = kind.model
    if kind.key == "tag":
        usage = sa.func.count(DocumentTag.document_id)
        query = sa.select(model.id, model.name, usage.label("documents")).outerjoin(
            DocumentTag, sa.and_(DocumentTag.tag_id == model.id, live_tag_links())
        )
    else:
        column = (
            Document.correspondent_id
            if kind.key == "correspondent"
            else Document.document_type_id
        )
        usage = sa.func.count(Document.id)
        query = sa.select(model.id, model.name, usage.label("documents")).outerjoin(
            Document, sa.and_(column == model.id, live())
        )

    rows = (
        await session.execute(
            query.where(
                model.library_id.in_(library_ids),
                # A name already merged away is not a candidate for merging again.
                model.merged_into_id.is_(None),
            )
            .group_by(model.id, model.name)
            .order_by(model.name)
        )
    ).all()
    return [
        {"id": str(row.id), "name": row.name, "documents": row.documents}
        for row in rows
    ]


async def propose(
    session: AsyncSession,
    library_ids: list[uuid.UUID],
    answerer,
    kind_key: str = "correspondent",
) -> UnifyProposal:
    """Ask which of these entries are the same thing. Changes nothing."""
    kind = KINDS[kind_key]
    names = await _current_names(session, kind, library_ids)
    if len(names) < 2:
        return UnifyProposal(groups=[], considered=len(names), kind=kind_key)
    if answerer is None or not answerer.available():
        return UnifyProposal(
            groups=[],
            considered=len(names),
            kind=kind_key,
            unavailable_reason=(
                "No API key is configured, so nothing can be proposed. The "
                "near-duplicate list on this page is found without one."
            ),
        )

    listing = "\n".join(
        f"- {entry['name']}  ({entry['documents']} documents)" for entry in names[:MAX_NAMES]
    )
    try:
        raw = await answerer.complete(kind.prompt + "\n\n" + listing)
    except Exception as error:
        log.error("could not propose unifications: %s", error)
        return UnifyProposal(
            groups=[], considered=len(names), kind=kind_key,
            unavailable_reason=f"Could not reach the model ({error}).",
        )

    return _parse(raw, names, getattr(answerer, "model", None), kind_key)


def _parse(
    raw: str, names: list[dict], model: str | None, kind_key: str = "correspondent"
) -> UnifyProposal:
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
            groups=[], considered=len(names), model=model, kind=kind_key,
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

    log.info(
        "proposed %s %s grouping(s) across %s names", len(groups), kind_key, len(names)
    )
    return UnifyProposal(
        groups=groups, considered=len(names), model=model, kind=kind_key
    )
