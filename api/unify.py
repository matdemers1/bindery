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
import re
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

# The answer is a JSON object listing groups, and a taxonomy with hundreds of
# near-duplicates produces a long one. The first run over 277 document types was
# cut off at the 4,000-token default and arrived as invalid JSON — which the
# parser correctly refused and unhelpfully described as "could not be read",
# sending the diagnosis to the wrong place entirely. Output tokens are the cheap
# half; there is no reason to be frugal here.
MAX_ANSWER_TOKENS = 16000

# Above this many names, ask in chunks. 697 tags in one request came back with
# `stop_reason=max_tokens` and *zero* characters of text — the whole budget went
# on reasoning about a list that long before a single group was written.
#
# Chunks are cut from the alphabetically-ordered list, which is exactly the
# ordering that would be wrong for the *candidate* list and is right here: tag
# duplication is overwhelmingly lexical — a plural, a prefix, a hyphen — so
# near-duplicates land in the same chunk. The overlap catches the pairs that
# straddle a boundary.
CHUNK_SIZE = 200
CHUNK_OVERLAP = 10


def _chunks(names: list[dict]) -> list[list[dict]]:
    if len(names) <= CHUNK_SIZE:
        return [names]
    out: list[list[dict]] = []
    start = 0
    while start < len(names):
        out.append(names[start : start + CHUNK_SIZE])
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return out

_SHARED_RULES = """
Rules:
- Group entries ONLY when you are confident they mean the same thing. Leave
  anything you are unsure about out entirely: a missed merge costs nothing, and
  a wrong one puts two different things in one folder.

- **A special case is not a duplicate.** If one name is a narrower version of
  another, they are DIFFERENT and must not be grouped at all — not in either
  direction. "Receipt" and "Veterinary Receipt" are two kinds. "Data Export"
  and "Survey Data Export" are two kinds. "Presentation" and "Training
  Presentation" are two kinds. Do not group a general name with a specific one
  merely because the specific one is more common in this archive.

- **Never make a narrower name the canonical one.** The survivor must be at
  least as general as every member.

- Group only names that differ in *wording*, not in *meaning*: word order, a
  plural, an abbreviation, punctuation, a synonym.

- Do NOT include a group you have decided against. If you considered some names
  and concluded they are different, simply omit them — do not list them with an
  explanation of why you left them out.

- Give a one-line reason for each group, in plain language.

Each entry is listed as `[id] name (count)`. Refer to entries by their **id**, never by
their name: two entries can carry the same name and only the id tells them apart.

Return JSON only, with no trailing commas, in exactly this shape:
{"groups": [{"canonical": "<id>", "members": ["<id>", "<id>"], "reason": "<why>"}]}
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

    proposals: list[UnifyProposal] = []
    for chunk in _chunks(names):
        # The id is shown because the name is not a key: two correspondents really can both be
        # called "VERIZON", and a listing of names alone cannot express that they are two rows.
        listing = "\n".join(
            f"- [{entry['id']}] {entry['name']}  ({entry['documents']} documents)"
            for entry in chunk[:MAX_NAMES]
        )
        try:
            raw = await answerer.complete(
                kind.prompt + "\n\n" + listing, max_tokens=MAX_ANSWER_TOKENS
            )
        except Exception as error:
            log.error("could not propose %s unifications: %s", kind_key, error)
            # One failed chunk must not discard the ones that worked. If every
            # chunk failed there is nothing to show, and the reason is the
            # useful thing to return.
            if len(_chunks(names)) == 1 or not proposals:
                return UnifyProposal(
                    groups=[], considered=len(names), kind=kind_key,
                    unavailable_reason=f"The model could not answer: {error}",
                )
            continue
        proposals.append(_parse(raw, chunk, getattr(answerer, "model", None), kind_key))

    if not proposals:
        return UnifyProposal(
            groups=[], considered=len(names), kind=kind_key,
            unavailable_reason="The model returned nothing usable.",
        )

    # A name can appear in two chunks via the overlap, and being merged twice is
    # a self-merge the second time. Keep the first group that claims it.
    claimed: set[str] = set()
    groups: list[ProposedGroup] = []
    for proposal in proposals:
        for group in proposal.groups:
            ids = {member["id"] for member in group.members}
            if ids & claimed:
                continue
            claimed |= ids
            groups.append(group)

    return UnifyProposal(
        groups=groups,
        considered=len(names),
        model=proposals[0].model,
        kind=kind_key,
    )


_TRAILING_COMMA = re.compile(r",\s*([}\]])")

# Phrases a model uses when it lists a group in order to explain that it is
# *not* a group. One real response contained
#     {"canonical": "Weapons Instructor Course",
#      "members": ["Weapons Instructor Course", "weapons configuration"],
#      "reason": "Left out - different concepts, not a true duplicate"}
# which only a trailing-comma parse error stopped from being applied. The prompt
# now forbids it; this is the belt to that pair of braces.
_NOT_A_GROUP = (
    "left out", "not a true duplicate", "not a duplicate", "excluding",
    "different concepts", "different things", "should not be merged",
    "do not merge", "unsure", "not grouped",
)


def _looks_like_a_refusal(reason: str) -> bool:
    lowered = reason.lower()
    return any(phrase in lowered for phrase in _NOT_A_GROUP)


def _is_narrower(member: str, canonical: str) -> bool:
    """Is `canonical` a special case of `member` rather than a rewording of it?

    Word containment, which is crude and catches the exact failure that
    happened: `Receipt` was merged into `Veterinary Receipt`, `Data Export` into
    `Survey Data Export`, `Presentation` into `Training Presentation`. In every
    one the survivor carried strictly more words, and the general kind was
    dissolved into a specific one because the specific one had more documents.

    False positives cost a merge that a person can still make by hand. False
    negatives cost a document type.
    """
    member_words = set(member.lower().replace("/", " ").split())
    canonical_words = set(canonical.lower().replace("/", " ").split())
    return bool(member_words) and member_words < canonical_words


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
    # A trailing comma is the most common way a model produces *nearly* valid
    # JSON, and rejecting the whole answer over one costs every group in it. One
    # real response was discarded for a comma before `]}`.
    text = _TRAILING_COMMA.sub(r"\1", text)

    try:
        payload = json.loads(text)
    except ValueError as error:
        # Log what actually came back. "Not JSON" on its own is unfalsifiable
        # from the outside, and this runs against a live model whose output
        # nobody can reproduce later.
        log.warning(
            "unification response was not JSON (%s); %s characters, "
            "starting %r and ending %r",
            error, len(text), text[:120], text[-120:],
        )
        return UnifyProposal(
            groups=[], considered=len(names), model=model, kind=kind_key,
            unavailable_reason="The model's answer could not be read as a proposal.",
        )

    by_id = {entry["id"]: entry for entry in names}

    # Names map to a *list*, not to one entry. Two rows whose names differ only in case are two
    # rows, and a dict keyed on the folded name silently kept one of them: the merge proposal then
    # covered one duplicate and left the other behind, which is the opposite of unifying.
    by_name: dict[str, list[dict]] = {}
    for entry in names:
        by_name.setdefault(entry["name"].strip().lower(), []).append(entry)

    def _resolve(reference: object) -> list[dict]:
        """Ids first, because a name is not unique. An unknown reference resolves to nothing."""
        token = str(reference).strip()
        entry = by_id.get(token)
        if entry is not None:
            return [entry]
        # A model that answers with names anyway must not cost us a row.
        return by_name.get(token.lower(), [])

    groups: list[ProposedGroup] = []

    for group in payload.get("groups", []):
        members: list[dict] = []
        seen: set[str] = set()
        for member_name in group.get("members", []):
            for entry in _resolve(member_name):
                if entry["id"] in seen:
                    continue
                seen.add(entry["id"])
                members.append(entry)

        # A group of one merges nothing.
        if len(members) < 2:
            continue

        reason = str(group.get("reason", "")).strip()
        if _looks_like_a_refusal(reason):
            log.info("dropping a group whose own reason rejects it: %r", reason[:80])
            continue

        canonical_reference = str(group.get("canonical", "")).strip()
        resolved = _resolve(canonical_reference)
        # The survivor must be one of the members: naming a row outside the group as canonical
        # would merge documents into something the group never mentioned.
        canonical = next((entry for entry in resolved if entry["id"] in seen), None)
        canonical_name = canonical["name"] if canonical is not None else canonical_reference
        if canonical is None:
            # The model named a survivor that does not exist. Rather than
            # invent it, keep the member carrying the most documents — the
            # merge is still correct, only the label is less pretty.
            canonical = max(members, key=lambda m: m["documents"])
            canonical_name = canonical["name"]

        narrower = [m["name"] for m in members if _is_narrower(m["name"], canonical_name)]
        if narrower:
            log.info(
                "dropping a group that would dissolve %s into the narrower %r",
                ", ".join(repr(n) for n in narrower), canonical_name,
            )
            continue

        groups.append(
            ProposedGroup(
                canonical=canonical_name,
                canonical_id=uuid.UUID(canonical["id"]),
                members=members,
                reason=reason,
            )
        )

    log.info(
        "proposed %s %s grouping(s) across %s names", len(groups), kind_key, len(names)
    )
    return UnifyProposal(
        groups=groups, considered=len(names), model=model, kind=kind_key
    )
