import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, created_at, uuid_pk


class Classification(Base):
    """One classification attempt. Never overwritten.

    > Without these tables, *Auditable Automation* is a slogan.

    `prompt_version` is what makes "reprocess everything still on v2" a query
    (REQ-113) rather than an $80 full re-run, and `structural_signals` is what
    makes the auto-file gate **reproducible from stored facts alone** (REQ-057):
    replaying the decision needs no model call, because the decision never
    depended on the model's opinion of itself.
    """

    __tablename__ = "classification"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("document.id"), nullable=False, index=True
    )
    model: Mapped[str] = mapped_column(sa.Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(sa.Text, nullable=False, index=True)

    # Artifacts under derived/<sha256>/classify/<prompt_version>.json — the exact
    # request and response, so a decision can be re-examined years later.
    request_artifact_path: Mapped[str | None] = mapped_column(sa.Text)
    response_artifact_path: Mapped[str | None] = mapped_column(sa.Text)

    # The model's self-reported per-field numbers. Displayed (REQ-065), never
    # decisive — LLM confidence is poorly calibrated.
    confidence: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    # The facts the gate actually read. See worker/classify/gate.py.
    structural_signals: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    gate_decision: Mapped[str | None] = mapped_column(sa.Text)
    gate_reasons: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )

    # Typed values a known form declared extractors for (REQ-041). Stored per
    # classification, not on the document: they are the output of one prompt
    # version, and a better prompt should produce a new set beside the old one.
    extracted_fields: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )

    # Cache health (REQ-053) and cost, straight from `usage`.
    usage: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = created_at()


class FieldProvenance(Base):
    """Why an AI-written field says what it says.

    **This is the why-panel.** Every AI-assigned field points at the exact page
    and sentence that justified it (REQ-049). A classification without evidence
    is a bug, not a degraded result.
    """

    __tablename__ = "field_provenance"

    id: Mapped[uuid.UUID] = uuid_pk()
    classification_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("classification.id"), nullable=False, index=True
    )
    field_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # Absolute page in the source file, so the why-panel can link straight to it.
    page_number: Mapped[int | None] = mapped_column(sa.Integer)
    snippet: Mapped[str | None] = mapped_column(sa.Text)
    confidence: Mapped[float | None] = mapped_column(sa.Float)
