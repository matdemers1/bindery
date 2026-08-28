"""Make the document/source-file library constraint deferrable.

`fk_document_source_file_library` enforces REQ-033 declaratively: a document's
library must match its source file's, so a segment can never drift into a
library its pages are not in.

Moving a file between libraries (REQ-102) has to change both sides, and there is
no order in which it can be done one row at a time — updating the documents
first breaks the constraint, and so does updating the file first, because
Postgres checks the parent side too. Deferring the check to commit lets the move
be atomic without weakening what is enforced: at every point an outside observer
can see, the invariant still holds.

`INITIALLY IMMEDIATE`, so ordinary writes still fail at the statement that
caused them rather than at commit. Only the move deliberately defers.

Revision ID: 0009_deferrable_library_fk
Revises: 0008_entities
Create Date: 2026-08-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009_deferrable_library_fk"
down_revision: str | Sequence[str] | None = "0008_entities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE document DROP CONSTRAINT fk_document_source_file_library")
    op.execute(
        "ALTER TABLE document ADD CONSTRAINT fk_document_source_file_library "
        "FOREIGN KEY (source_file_id, library_id) "
        "REFERENCES source_file (id, library_id) "
        "DEFERRABLE INITIALLY IMMEDIATE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE document DROP CONSTRAINT fk_document_source_file_library")
    op.execute(
        "ALTER TABLE document ADD CONSTRAINT fk_document_source_file_library "
        "FOREIGN KEY (source_file_id, library_id) "
        "REFERENCES source_file (id, library_id)"
    )
