"""The one definition of "is this row in a locked vault" (T-16.4, REQ-180).

There are two boundary modules in this application — `api/db/scope.py` for
routes that take a `Scope`, and `api/db/repository.py` for the ones that take a
`user_id` — and a vault filter written separately in each is two filters that
will disagree. The leak suite found exactly that: `scope.py` was updated, the
repository was not, and five read paths went straight past it.

So the clause lives here and both import it. Not a helper for convenience: a
single definition, because the failure mode of two is silent.
"""

import uuid

import sqlalchemy as sa

from api.db.models import Document
from api.segments import live
from api.vault.session import sessions


def is_unlocked(user_id: uuid.UUID) -> bool:
    return sessions.is_unlocked(user_id)


def document_clause(user_id: uuid.UUID, *, unlocked: bool | None = None):
    """Documents this caller may see, vault-wise.

    Note what this is not: not `vaulted_by IS NULL OR vaulted_by = me`. A
    vaulted document is invisible **to its owner too** until the PIN is
    entered — that is the whole feature. Whose vault it is only matters once
    the vault is actually open.
    """
    if unlocked is None:
        unlocked = is_unlocked(user_id)
    if unlocked:
        return sa.or_(Document.vaulted_by.is_(None), Document.vaulted_by == user_id)
    return Document.vaulted_by.is_(None)


def hidden_source_file_ids(user_id: uuid.UUID, *, unlocked: bool | None = None):
    """Files to hide, as a subquery.

    A file and a document are different rows. Hiding the document and listing
    the file leaves the page images downloadable and the OCR text servable,
    which is most of what was being hidden.
    """
    if unlocked is None:
        unlocked = is_unlocked(user_id)
    query = sa.select(Document.source_file_id).where(
        Document.vaulted_by.is_not(None), live()
    )
    if unlocked:
        query = query.where(Document.vaulted_by != user_id)
    return query
