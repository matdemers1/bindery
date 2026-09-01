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
    """Documents this caller may see in an ordinary view, vault-wise.

    **A vaulted document is never one of them**, unlocked or not. It lives on
    the vault screen and in vault search, and nowhere else — not the archive,
    not photos, not files, not search, not Ask, not a count or a facet.

    The first version let them back into every view while the vault was open,
    on the reasoning that "unlocked, they behave like everything else". That is
    wrong, and it is wrong in the way that matters: the reason to put a
    document in the vault is that you do not want it on screen when somebody is
    looking over your shoulder, and the vault is open for fifteen minutes after
    you glance at it. A feature whose privacy depends on remembering to lock it
    is not one people can rely on.

    `unlocked` is still accepted so callers do not have to change and so the
    session lookup can be avoided where the caller already knows — but it no
    longer changes the answer. Being open governs whether the vault can be
    *read*, not whether its contents leak into everything else.
    """
    return Document.vaulted_by.is_(None)


def hidden_source_file_ids(user_id: uuid.UUID, *, unlocked: bool | None = None):
    """Files to hide from ordinary views, as a subquery.

    A file and a document are different rows. Hiding the document and listing
    the file leaves the page images downloadable and the OCR text servable,
    which is most of what was being hidden.

    Hidden whether or not the vault is open, for the reason `document_clause`
    gives.
    """
    return sa.select(Document.source_file_id).where(
        Document.vaulted_by.is_not(None), live()
    )
