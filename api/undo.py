"""Generic undo, driven by the audit trail (REQ-067).

> Every automated action is undoable, with the undo affordance present at the
> moment of the action.

`audit_event.before` / `after` are what make this mechanical rather than bespoke:
undoing an action is restoring the `before` it recorded, and the undo is itself
recorded so it can be undone in turn. Nothing is deleted at any point.

Only fields the recording action actually touched are restored. An undo that
reset everything would quietly discard edits made since, which is a worse
failure than the one it is fixing.
"""

import uuid
from dataclasses import dataclass
from datetime import date

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api import field_source
from api.audit import record
from api.db.enums import ActorType, ReviewState, Sensitivity
from api.db.models import AuditEvent, Document, DocumentTag

# The four endpoints that walk something back. Named here rather than spelled
# out at each entry so the registry can be checked against the app's own route
# table — a route that is renamed and not updated here is a promise the Why
# panel keeps making after the endpoint stopped existing.
DOCUMENT_UNDO = "/api/documents/{document_id}/undo"
BULK_UNDO = "/api/bulk/{operation_id}/undo"
MERGE_UNDO = "/api/merges/{operation_id}/undo"
SEGMENT_UNDO = "/api/files/{source_file_id}/segments/undo"


@dataclass(frozen=True)
class Undoable:
    """How one audited action is walked back.

    `fields` is meaningful only for the generic document path, which restores
    named columns from the event's `before`. The other three reversals are
    whole-operation and read their own manifests.
    """

    entity_type: str
    route: str
    fields: tuple[str, ...] = ()


# **Every audited action appears here or in `NOT_UNDOABLE`, and
# `tests/test_undo_registry.py` fails when a new one appears in neither.**
#
# Undo is the trust surface — the kill criterion is loss of trust in the
# automated filing — so an action that is recorded but quietly not reversible
# is a promise the UI makes and this module does not keep. Before the registry
# the answer lived in four places: a literal here, a count in `api/bulk.py`, a
# second count in `api/entities.py`, and a sequence chain in `api/segments.py`,
# with nothing that noticed a fifth action arriving. It still lives in four
# implementations; what the registry adds is one list of what they cover, and a
# guard that fails when something joins none of them.
REGISTRY: dict[str, Undoable] = {
    # -- the generic document path (`undo_event`, below) -------------------
    #
    # `route_to_review` is deliberately absent: "this needs your attention" is
    # not a decision, so there is nothing in it to walk back.
    "classify": Undoable(
        "document", DOCUMENT_UNDO,
        ("title", "summary", "document_date", "correspondent_id",
         "document_type_id", "review_state"),
    ),
    "rule_applied": Undoable(
        "document", DOCUMENT_UNDO, ("document_type_id", "sensitivity"),
    ),
    "file": Undoable("document", DOCUMENT_UNDO, ("review_state",)),
    "edit": Undoable(
        "document", DOCUMENT_UNDO,
        ("title", "summary", "document_date", "correspondent_id",
         "document_type_id", "sensitivity", "review_state"),
    ),
    # -- whole-operation reversals, each with its own endpoint -------------
    "bulk_edit": Undoable("bulk_operation", BULK_UNDO),
    "merge_correspondent": Undoable("correspondent", MERGE_UNDO),
    "merge_tag": Undoable("tag", MERGE_UNDO),
    "merge_document_type": Undoable("document_type", MERGE_UNDO),
    "segment": Undoable("source_file", SEGMENT_UNDO),
}

# Actions that are recorded and cannot be walked back, each with the reason.
#
# A reason, not a bare list: "not undoable" is a product decision, and the next
# person to read it needs to know whether they are looking at something nobody
# got to or something that genuinely has no reverse. Making one of these
# undoable is a feature, not a fix.
NOT_UNDOABLE: dict[str, str] = {
    # Records of something that happened, with no state to put back.
    "login": "a sign-in is an event, not a change; there is nothing to restore.",
    "logout": "ending a session is an event; signing in again is how it comes back, "
              "and restoring a revoked session would be the opposite of the point.",
    "oidc_linked": "connecting D3 Auth is reversed by disconnecting it in Settings, "
                   "which asks for the local password — not by an undo button that "
                   "would not.",
    "oidc_unlinked": "disconnecting is reversed by connecting again, which needs the "
                     "provider to say who you are; the link row is tombstoned either way.",
    "admin_granted": "administrator rights are granted and withdrawn where they are "
                     "managed — People here, or the grant at the provider — and a "
                     "one-click undo of a permission change is how permissions drift.",
    "admin_revoked": "the same in the other direction.",
    "ingest": "originals are immutable and never deleted (invariant 1 and 3), "
              "so an arrival cannot be taken back.",
    "scan": "a backlog scan reads a folder and writes only its own inventory; "
            "nothing about the archive changed.",
    "route_to_review": "\"this needs your attention\" is not a decision, so "
                       "there is nothing in it to walk back — and the gate's "
                       "other verdict, `file`, is undoable.",
    "integrity_check": "a read-only pass over the pool; it changes nothing.",
    "export_full": "an export writes a derived tree beside the archive and "
                   "changes nothing in it; delete the folder instead.",
    "export_go_bag": "as with a full export, the archive itself is untouched.",
    "backup_run": "a backup only reads; the artefact it wrote is the "
                  "operator's to remove.",
    "offsite_replicate_requested": "the objects are already offsite by the time "
                                   "anyone could press undo, and the offsite "
                                   "ledger is append-only.",
    "retry": "re-running a stage is a request for work, not a decision; what "
             "the work then decides is undoable where it lands.",
    "rescan_requested": "as with retry — the rescan's own results are what "
                        "carry an undo.",
    "reclassify_requested": "the classification it produces is undoable; the "
                            "request to produce one is not a change to reverse.",
    # Each other's reverse, deliberately as a second deliberate act rather than
    # a one-click undo.
    "enable": "a rule is switched off again on the rule screen; both "
              "directions are ordinary, audited actions.",
    "disable": "a rule is switched back on on the rule screen.",
    "account_suspended": "an account is brought back by restoring it, which is "
                         "its own decision and its own audit row.",
    "account_restored": "an account is suspended again by suspending it.",
    "acknowledge_job": "the same button un-acknowledges it, which is the "
                       "reverse and is itself audited.",
    "unacknowledge_job": "the same button acknowledges it again.",
    "import_to_vault": "the destination of an import is changed by choosing "
                       "the other one; nothing has been sealed yet.",
    "import_not_to_vault": "the destination of an import is changed by "
                           "choosing the other one.",
    # Secrets and credentials: what an undo would need is not kept.
    "change_password": "the previous password is not stored, by design.",
    "password_reset": "the previous password is not stored, by design.",
    "reset_code_issued": "a code is spent or expires; it cannot be un-issued.",
    "token_issued": "the secret was shown once and cannot be un-shown. Revoke "
                    "it instead.",
    "token_revoked": "a revoked secret stays revoked; issue a new token.",
    "totp_enrolled": "a second factor is set up with the device in hand, and "
                     "removing it needs the same deliberate step.",
    "totp_disabled": "re-enrolling needs the device; that is the step, not an "
                     "undo.",
    "invite_created": "an invite is withdrawn by revoking it, which is its own "
                      "audited action.",
    "invite_revoked": "a revoked invite is never re-armed; issue a new one.",
    "account_unlocked": "clearing a login lockout releases a throttle counter "
                        "and nothing else — `api/auth/throttle.py` faces the "
                        "open internet and re-locking on demand is not a "
                        "capability worth having.",
    # Administrative settings. The previous value is in the event's `before`,
    # so the reversal is to set it again — visibly, as an administrator.
    "quota_changed": "the old limit is in the audit row; an administrator "
                     "sets it back, so the change is never silent.",
    "update_settings": "the old value is in the audit row and settings are "
                       "re-set rather than rewound.",
    "membership_changed": "who may read a library is a household decision, "
                          "and the previous role is in the audit row.",
    "admin_granted": "administration is granted and withdrawn by a person, "
                     "never by a one-click reversal.",
    # Creation. Nothing is ever automatically deleted (invariant 3), so an
    # undo here would be the one destructive path this project forbids.
    "create": "nothing is ever automatically deleted (invariant 3), so "
              "creating a rule, an asset or a library has no reverse here.",
    "account_created": "an account is suspended, never deleted (invariant 3).",
    "library_created": "a library is not deleted (invariant 3).",
    "vault_created": "a vault is not deleted (invariant 3).",
    "attach_asset": "attaching a document to an asset is a person's own "
                    "filing, and the link is additive — nothing was "
                    "overwritten for an undo to put back.",
    # The vault. Every one of these needs the passphrase, which is the point.
    "vault_unlocked": "the reverse is locking the vault, which is one click "
                      "on the vault screen and does not belong behind an "
                      "undo affordance.",
    "vault_move_in": "moving a document back out is its own decision on the "
                     "vault screen: it needs the vault open and it rewrites "
                     "the plaintext. Not something to offer as one click "
                     "beside an automated action.",
    "vault_move_out": "moving it back in destroys the plaintext again, which "
                      "is not a thing to do by pressing undo.",
    "vault_in": "the sweep seals what a vault-bound import asked for; "
                "reversing it is a move-out, with the passphrase.",
    # A move is file-scoped and taxonomy does not travel with it.
    "moved_library": "moving a file back is the same operation in reverse, "
                     "and it is a person's decision about where a file "
                     "belongs — the revoked cross-library tags are named in "
                     "the audit `before` so nothing is lost either way.",
    # Undos themselves. Reversing an undo is making the decision again.
    "undo": "an undo is walked back by making the decision again; undoing the "
            "undo would give the chain two heads.",
    "bulk_undo": "as with `undo` — re-run the bulk edit.",
    "undo_merge": "as with `undo` — merge them again.",
    "segment_undo": "the segment chain already reverses in both directions: "
                    "undoing again walks one step further back, which is what "
                    "`api/segments.undo` does.",
}

# The document-field map the generic path restores from, derived from the
# registry so there is one list rather than two that can disagree.
UNDOABLE = {
    action: entry.fields
    for action, entry in REGISTRY.items()
    if entry.route == DOCUMENT_UNDO
}

_COERCE = {
    "review_state": ReviewState,
    "sensitivity": Sensitivity,
    "correspondent_id": lambda value: uuid.UUID(value) if value else None,
    "document_type_id": lambda value: uuid.UUID(value) if value else None,
    "document_date": lambda value: date.fromisoformat(value) if value else None,
}


class UndoError(ValueError):
    """This action cannot be walked back."""


async def already_undone(
    session: AsyncSession, document_id: uuid.UUID
) -> set[uuid.UUID]:
    """Event ids that a previous undo has already walked back.

    Every undo records which event it reversed, so this needs no extra state —
    but it does need to be consulted, or pressing undo twice reverses the same
    action twice and the chain never advances.
    """
    rows = (
        await session.execute(
            sa.select(AuditEvent.after).where(
                AuditEvent.entity_type == "document",
                AuditEvent.entity_id == document_id,
                AuditEvent.action == "undo",
            )
        )
    ).scalars().all()

    undone: set[uuid.UUID] = set()
    for payload in rows:
        raw = (payload or {}).get("undid_event")
        if raw:
            try:
                undone.add(uuid.UUID(raw))
            except ValueError:
                continue
    return undone


async def latest_undoable(
    session: AsyncSession, document_id: uuid.UUID
) -> AuditEvent | None:
    """The most recent decision that has not already been walked back."""
    undone = await already_undone(session, document_id)
    conditions = [
        AuditEvent.entity_type == "document",
        AuditEvent.entity_id == document_id,
        AuditEvent.action.in_(tuple(UNDOABLE)),
    ]
    if undone:
        conditions.append(AuditEvent.id.not_in(undone))

    return (
        await session.execute(
            sa.select(AuditEvent)
            .where(sa.and_(*conditions))
            # By sequence, not timestamp: several events can share a transaction.
            .order_by(AuditEvent.sequence.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def undo_event(
    session: AsyncSession, event: AuditEvent, *, actor_id: uuid.UUID | None
) -> Document:
    if event.action not in UNDOABLE:
        raise UndoError(f"{event.action!r} is not an undoable action")

    document = await session.get(Document, event.entity_id)
    if document is None:
        raise UndoError("the document no longer exists")

    before = event.before or {}
    restored: dict[str, object] = {}
    current: dict[str, object] = {}

    for name in UNDOABLE[event.action]:
        if name not in before:
            continue
        value = before[name]
        coerce = _COERCE.get(name)
        current[name] = as_json(getattr(document, name))
        setattr(document, name, coerce(value) if coerce and value is not None else value)
        restored[name] = value

    undo_event_row = await record(
        session,
        entity_type="document",
        entity_id=document.id,
        action="undo",
        actor_type=ActorType.HUMAN,
        actor_id=actor_id,
        before=current,
        after={"undid": event.action, "undid_event": str(event.id), "restored": restored},
    )
    await session.flush()

    if event.action == "classify":
        # Take back the tags the classifier applied. Only AI-sourced links: a
        # tag a human or a rule set is not the classifier's to withdraw. The
        # links are superseded, not deleted, so the record of what was applied
        # survives the undo.
        result = await session.execute(
            sa.update(DocumentTag)
            .where(
                DocumentTag.document_id == document.id,
                DocumentTag.source == ActorType.AI.value,
                DocumentTag.removed_at.is_(None),
            )
            .values(removed_at=sa.func.now(), removed_by_event_id=undo_event_row.id)
        )
        # An undone classification goes back to the queue, not to a
        # pre-classification limbo — that is what "returns to review" means.
        document.review_state = ReviewState.NEEDS_REVIEW
        restored["review_state"] = ReviewState.NEEDS_REVIEW.value
        undo_event_row.after = {
            **(undo_event_row.after or {}),
            "restored": restored,
            "tags_withdrawn": result.rowcount or 0,
        }

    if event.action == "edit":
        # Give the fields back, or the value stays frozen at something nobody
        # chose: held by a person whose decision has just been reversed, and so
        # still off limits to classification. Released rather than deleted, so
        # "somebody set this and then took it back" stays answerable.
        await field_source.release(
            session, document.id, list(restored), event_id=undo_event_row.id
        )
        # Tags the edit revoked come back, and tags it added go away again,
        # read from the manifest the edit recorded rather than inferred from
        # timestamps — inference would sweep up tags a *later* edit added.
        # Without this the removal stands, and the removal is exactly what
        # stops classification re-applying the tag, so the undo would silently
        # do half its job.
        after = event.after or {}
        if put_back := after.get("tag_ids_removed"):
            await session.execute(
                sa.update(DocumentTag)
                .where(
                    DocumentTag.document_id == document.id,
                    DocumentTag.tag_id.in_([uuid.UUID(t) for t in put_back]),
                )
                .values(removed_at=None, removed_by_event_id=None)
            )
        if take_away := after.get("tag_ids_added"):
            await session.execute(
                sa.update(DocumentTag)
                .where(
                    DocumentTag.document_id == document.id,
                    DocumentTag.tag_id.in_([uuid.UUID(t) for t in take_away]),
                    DocumentTag.removed_at.is_(None),
                )
                .values(removed_at=sa.func.now(), removed_by_event_id=undo_event_row.id)
            )

    await session.flush()
    return document


def as_json(value: object) -> object:
    """A document field in the wire form `_COERCE` reads back.

    Public because the actions that record a `before` have to write it in this
    shape or the undo restores something the column cannot hold — an undo that
    raises is no better than the undo that restored nothing.
    """
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, uuid.UUID | date):
        return str(value)
    return getattr(value, "value", str(value))
