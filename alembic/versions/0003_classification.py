"""Classification, provenance, rules, and embeddings.

The tables that make *Auditable Automation* real rather than aspirational:
`classification` records every attempt and never overwrites, `field_provenance`
records why each AI-written field says what it says, and the gate's structural
signals are stored alongside its decision so the decision is **reproducible
without another model call** (REQ-057).

Revision ID: 0003_classification
Revises: 0002_segments
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0003_classification"
down_revision: str | None = "0002_segments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIMENSIONS = 512


def upgrade() -> None:
    now = sa.text("now()")
    uuid_col = postgresql.UUID(as_uuid=True)

    op.create_table(
        "document_type",
        sa.Column("id", uuid_col, primary_key=True),
        sa.Column("library_id", uuid_col),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["library_id"], ["library.id"], name="fk_document_type_library_id_library"
        ),
    )
    op.create_index("ix_document_type_library_id", "document_type", ["library_id"])
    op.execute(
        "ALTER TABLE document_type ADD CONSTRAINT uq_document_type_library_id_slug "
        "UNIQUE NULLS NOT DISTINCT (library_id, slug)"
    )

    op.create_table(
        "correspondent",
        sa.Column("id", uuid_col, primary_key=True),
        sa.Column("library_id", uuid_col),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text()),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["library_id"], ["library.id"], name="fk_correspondent_library_id_library"
        ),
    )
    op.create_index("ix_correspondent_library_id", "correspondent", ["library_id"])
    op.execute(
        "ALTER TABLE correspondent ADD CONSTRAINT uq_correspondent_library_id_slug "
        "UNIQUE NULLS NOT DISTINCT (library_id, slug)"
    )
    # Typo tolerance on names — the "Hoda" → "Honda" path (REQ-023) finally has
    # correspondents to match against.
    op.create_index(
        "ix_correspondent_name_trgm", "correspondent", ["name"],
        postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"},
    )

    op.create_table(
        "rule",
        sa.Column("id", uuid_col, primary_key=True),
        sa.Column("library_id", uuid_col, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        # Disabled by default: a rule must be dry-run before it can act (REQ-061).
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column(
            "conditions", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "actions", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(["library_id"], ["library.id"], name="fk_rule_library_id_library"),
    )
    op.create_index("ix_rule_library_id", "rule", ["library_id"])

    op.create_table(
        "classification",
        sa.Column("id", uuid_col, primary_key=True),
        sa.Column("document_id", uuid_col, nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("request_artifact_path", sa.Text()),
        sa.Column("response_artifact_path", sa.Text()),
        sa.Column(
            "confidence", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "structural_signals", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("gate_decision", sa.Text()),
        sa.Column(
            "gate_reasons", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "usage", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["document_id"], ["document.id"], name="fk_classification_document_id_document"
        ),
    )
    op.create_index("ix_classification_document_id", "classification", ["document_id"])
    # "Reprocess everything still on v2" is a query (REQ-113).
    op.create_index("ix_classification_prompt_version", "classification", ["prompt_version"])

    op.create_table(
        "field_provenance",
        sa.Column("id", uuid_col, primary_key=True),
        sa.Column("classification_id", uuid_col, nullable=False),
        sa.Column("field_name", sa.Text(), nullable=False),
        sa.Column("page_number", sa.Integer()),
        sa.Column("snippet", sa.Text()),
        sa.Column("confidence", sa.Float()),
        sa.ForeignKeyConstraint(
            ["classification_id"], ["classification.id"],
            name="fk_field_provenance_classification_id_classification",
        ),
    )
    op.create_index(
        "ix_field_provenance_classification_id", "field_provenance", ["classification_id"]
    )

    op.add_column("document", sa.Column("document_type_id", uuid_col))
    op.add_column("document", sa.Column("correspondent_id", uuid_col))
    op.add_column("document", sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS)))
    op.create_foreign_key(
        "fk_document_document_type_id_document_type",
        "document", "document_type", ["document_type_id"], ["id"],
    )
    op.create_foreign_key(
        "fk_document_correspondent_id_correspondent",
        "document", "correspondent", ["correspondent_id"], ["id"],
    )
    op.create_index("ix_document_document_type_id", "document", ["document_type_id"])
    op.create_index("ix_document_correspondent_id", "document", ["correspondent_id"])

    # HNSW over cosine distance, for neighbour retrieval only. Search does not
    # touch this index — semantic search was declined (ADR-004).
    op.execute(
        "CREATE INDEX ix_document_embedding_hnsw ON document "
        "USING hnsw (embedding vector_cosine_ops) WHERE superseded_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_document_embedding_hnsw")
    op.drop_index("ix_document_correspondent_id", table_name="document")
    op.drop_index("ix_document_document_type_id", table_name="document")
    op.drop_constraint(
        "fk_document_correspondent_id_correspondent", "document", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_document_document_type_id_document_type", "document", type_="foreignkey"
    )
    op.drop_column("document", "embedding")
    op.drop_column("document", "correspondent_id")
    op.drop_column("document", "document_type_id")
    op.drop_table("field_provenance")
    op.drop_table("classification")
    op.drop_table("rule")
    op.drop_table("correspondent")
    op.drop_table("document_type")
