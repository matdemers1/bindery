"""Correspondent aliases, assets, saved searches, and duplicate links.

The through-line is **merge**. Merging correspondents or tags rewrites link rows
across thousands of documents, so every one of these tables is shaped so that a
merge can be previewed, executed in one transaction, recorded as one operation
with a manifest, and undone as a single action.

Aliases exist so merge stays rare: *AMERICAN HONDA FINANCE*, *Honda Financial
Services* and *Honda Fin Svcs* resolving to one record at classification time is
better than reconciling them afterwards.

Revision ID: 0008_entities
Revises: 0007_import_session
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_entities"
down_revision: str | None = "0007_import_session"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ASSET_KINDS = ("vehicle", "property", "policy", "account", "person", "other")


def upgrade() -> None:
    now = sa.text("now()")
    uuid_col = postgresql.UUID(as_uuid=True)
    postgresql.ENUM(*ASSET_KINDS, name="asset_kind").create(op.get_bind(), checkfirst=True)

    # Aliases keep merge rare rather than constant.
    op.create_table(
        "correspondent_alias",
        sa.Column("id", uuid_col, primary_key=True),
        sa.Column("correspondent_id", uuid_col, nullable=False),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["correspondent_id"], ["correspondent.id"],
            name="fk_correspondent_alias_correspondent_id_correspondent",
        ),
        sa.UniqueConstraint("correspondent_id", "slug",
                            name="uq_correspondent_alias_correspondent_id_slug"),
    )
    op.create_index("ix_correspondent_alias_slug", "correspondent_alias", ["slug"])
    op.create_index(
        "ix_correspondent_alias_trgm", "correspondent_alias", ["alias"],
        postgresql_using="gin", postgresql_ops={"alias": "gin_trgm_ops"},
    )

    # Merge tombstones rather than deletes: a merged-away correspondent keeps
    # pointing at its survivor so undo is a flag flip and old links still resolve.
    op.add_column("correspondent", sa.Column("merged_into_id", uuid_col))
    op.add_column("correspondent", sa.Column("merged_at", sa.DateTime(timezone=True)))
    op.create_foreign_key(
        "fk_correspondent_merged_into_id_correspondent",
        "correspondent", "correspondent", ["merged_into_id"], ["id"],
    )
    op.add_column("tag", sa.Column("merged_into_id", uuid_col))
    op.add_column("tag", sa.Column("merged_at", sa.DateTime(timezone=True)))
    op.create_foreign_key("fk_tag_merged_into_id_tag", "tag", "tag", ["merged_into_id"], ["id"])

    op.create_table(
        "asset",
        sa.Column("id", uuid_col, primary_key=True),
        sa.Column("library_id", uuid_col, nullable=False),
        sa.Column("kind", postgresql.ENUM(*ASSET_KINDS, name="asset_kind", create_type=False),
                  nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        # VIN, plate, address, policy number.
        sa.Column("attributes", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        # Stable external id, so a future VaultDrv link needs no migration.
        sa.Column("external_ref", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(["library_id"], ["library.id"], name="fk_asset_library_id_library"),
        sa.UniqueConstraint("library_id", "slug", name="uq_asset_library_id_slug"),
    )
    op.create_index("ix_asset_library_id", "asset", ["library_id"])
    op.create_index("ix_asset_name_trgm", "asset", ["name"],
                    postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"})

    # Many-to-many: one document can concern several assets.
    op.create_table(
        "document_asset",
        sa.Column("document_id", uuid_col, primary_key=True),
        sa.Column("asset_id", uuid_col, primary_key=True),
        sa.Column("source", postgresql.ENUM(name="tag_source", create_type=False), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"],
                                name="fk_document_asset_document_id_document"),
        sa.ForeignKeyConstraint(["asset_id"], ["asset.id"], name="fk_document_asset_asset_id_asset"),
    )
    op.create_index("ix_document_asset_asset_id", "document_asset", ["asset_id"])

    # A shelf and a packet are the same mechanism; a packet is a shelf with an
    # export button.
    op.create_table(
        "saved_search",
        sa.Column("id", uuid_col, primary_key=True),
        sa.Column("library_id", uuid_col, nullable=False),
        sa.Column("user_id", uuid_col, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("query", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("is_shelf", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_packet", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(["library_id"], ["library.id"],
                                name="fk_saved_search_library_id_library"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"],
                                name="fk_saved_search_user_id_app_user"),
        sa.UniqueConstraint("user_id", "library_id", "name",
                            name="uq_saved_search_user_id_library_id_name"),
    )
    op.create_index("ix_saved_search_library_id", "saved_search", ["library_id"])

    # Near-duplicates are recorded, never auto-resolved: two scans of the same
    # deed at different qualities are both worth keeping until a human says not.
    op.create_table(
        "duplicate_pair",
        sa.Column("id", uuid_col, primary_key=True),
        sa.Column("library_id", uuid_col, nullable=False),
        sa.Column("document_a_id", uuid_col, nullable=False),
        sa.Column("document_b_id", uuid_col, nullable=False),
        sa.Column("similarity", sa.Float(), nullable=False),
        sa.Column("dismissed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(["library_id"], ["library.id"],
                                name="fk_duplicate_pair_library_id_library"),
        sa.ForeignKeyConstraint(["document_a_id"], ["document.id"],
                                name="fk_duplicate_pair_document_a_id_document"),
        sa.ForeignKeyConstraint(["document_b_id"], ["document.id"],
                                name="fk_duplicate_pair_document_b_id_document"),
        sa.CheckConstraint("document_a_id < document_b_id",
                           name="ck_duplicate_pair_ordered"),
        sa.UniqueConstraint("document_a_id", "document_b_id",
                            name="uq_duplicate_pair_document_a_id_document_b_id"),
    )
    op.create_index("ix_duplicate_pair_library_id", "duplicate_pair", ["library_id"])


def downgrade() -> None:
    op.drop_table("duplicate_pair")
    op.drop_table("saved_search")
    op.drop_table("document_asset")
    op.drop_table("asset")
    op.drop_constraint("fk_tag_merged_into_id_tag", "tag", type_="foreignkey")
    op.drop_column("tag", "merged_at")
    op.drop_column("tag", "merged_into_id")
    op.drop_constraint("fk_correspondent_merged_into_id_correspondent", "correspondent",
                       type_="foreignkey")
    op.drop_column("correspondent", "merged_at")
    op.drop_column("correspondent", "merged_into_id")
    op.drop_table("correspondent_alias")
    postgresql.ENUM(name="asset_kind").drop(op.get_bind(), checkfirst=True)
