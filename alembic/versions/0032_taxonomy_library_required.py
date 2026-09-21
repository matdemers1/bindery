"""Taxonomy belongs to exactly one library, and the column now says so (BND-FR-006).

`tag`, `correspondent` and `document_type` permitted `library_id IS NULL`, and
four read paths deliberately widened to include such rows — `sa.or_(…in_(…),
…is_(None))` in the classifier's candidate list, its id resolver, search's tag
filter and the taxonomy health panel. A row with a null library is a row that
belongs to every account at once, which is the exact shape ADR-005 exists to
prevent: the library is the access boundary, and it is not a boundary if a row
can stand outside it.

Nothing has ever created one. Every write site — `api/editing.py`,
`api/bulk.py`, `worker/stages/rules.py` and the classifier's `_get_or_create` —
takes the library from the document it is filing. So this is a dormant path, and
the cheapest moment to close a dormant path is while it is still dormant: one
`INSERT` with a null, by a future feature that meant "shared vocabulary", and
every household member's tags become visible to every other.

**This migration refuses rather than guesses.** If a null row exists, there is no
correct library to move it to — only the operator knows whether it was meant to
be shared — so it names the rows and stops. A migration that failed the boot is
the designed behaviour here (invariant 10); silently assigning a library would
be inventing an answer about who can see what.
"""

import sqlalchemy as sa
from alembic import op

revision = "0032_taxonomy_library_required"
down_revision = "0031_oidc_identities"
branch_labels = None
depends_on = None

TABLES = ("tag", "correspondent", "document_type")


def upgrade() -> None:
    bind = op.get_bind()
    stranded: list[str] = []
    for table in TABLES:
        rows = bind.execute(
            sa.text(f"select id, name from {table} where library_id is null order by name")  # noqa: S608
        ).all()
        stranded.extend(f"{table}: {row.name} ({row.id})" for row in rows)

    if stranded:
        raise RuntimeError(
            "these taxonomy rows have no library, so they are readable from every "
            "account (BND-FR-006). Assign each one a library, then run this "
            "migration again — it will not choose for you:\n  " + "\n  ".join(stranded)
        )

    for table in TABLES:
        op.alter_column(table, "library_id", existing_type=sa.dialects.postgresql.UUID(), nullable=False)


def downgrade() -> None:
    for table in TABLES:
        op.alter_column(table, "library_id", existing_type=sa.dialects.postgresql.UUID(), nullable=True)
