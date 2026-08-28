import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, uuid_pk


class Page(Base):
    """The unit of search (Architecture L5).

    A 100-page bundle produces 100 rows, each independently addressable. This is
    the decision the whole product rests on.
    """

    __tablename__ = "page"
    __table_args__ = (sa.UniqueConstraint("source_file_id", "page_number"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    source_file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("source_file.id"), nullable=False, index=True
    )
    page_number: Mapped[int] = mapped_column(sa.Integer, nullable=False)  # 1-indexed
    text: Mapped[str | None] = mapped_column(sa.Text)
    # Generated, so it can never drift out of sync with `text`. GIN indexed —
    # this is the primary search index.
    text_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        sa.Computed("to_tsvector('english', coalesce(text, ''))", persisted=True),
    )
    word_boxes_path: Mapped[str | None] = mapped_column(sa.Text)
    render_path: Mapped[str | None] = mapped_column(sa.Text)
    thumb_path: Mapped[str | None] = mapped_column(sa.Text)
