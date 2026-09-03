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


def document_clause(_caller: uuid.UUID | None = None, /):
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

    An `unlocked` argument used to be accepted here and ignored (CR-084). A
    parameter that is read by nothing and reads like it governs the boundary is
    the shape that caused the worst leak this module exists to prevent: the
    next person adding a "but show them while it is open" view would have
    passed `unlocked=True`, got no error, and shipped something that looked
    deliberate in review. It is gone, so that call is now a `TypeError`. Being
    open governs whether the vault can be *read*, not whether its contents leak
    into everything else.

    The caller is accepted positionally, and its name says what the docstring
    says: it does not reach the answer. Fourteen call sites pass it, and a
    signature that reads as per-user filtering when the clause is global is the
    other half of the same complaint — so it cannot be passed by keyword, where
    it would read as meaningful.
    """
    return Document.vaulted_by.is_(None)


def hidden_source_file_ids(_caller: uuid.UUID | None = None, /):
    """Files to hide from ordinary views, as a subquery.

    A file and a document are different rows. Hiding the document and listing
    the file leaves the page images downloadable and the OCR text servable,
    which is most of what was being hidden.

    Hidden whether or not the vault is open, for the reason `document_clause`
    gives — and whether or not the vaulted row is still the live one.

    That second half was a hole (CR-073). This filtered `live()`, on the
    reasonable-looking premise that history hides nothing; but what a seal
    destroys is the *file*, and superseding the vaulted row does not bring the
    plaintext back. A re-segmentation over a vaulted document therefore
    un-hid its file while the ciphertext, the `vaulted_by` mark and the missing
    original all stayed exactly as they were — the file returned to search,
    photos and the file list as a row with nothing behind it. `vaulted_by` is
    the only thing that says a file has been sealed, so it is the only thing
    asked here.
    """
    return sa.select(Document.source_file_id).where(Document.vaulted_by.is_not(None))
