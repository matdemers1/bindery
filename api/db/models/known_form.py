import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, created_at, uuid_pk


class KnownForm(Base):
    """A deterministically recognisable document type.

    > A registry match is a fact, not an opinion.

    This is what turns "find my DD-214" from a ranked guess into a certainty.
    `match_rules` holds footer form numbers, characteristic phrases and layout
    signatures — all deterministic, none of it a model's judgement.

    **Precision is prioritised over recall** (REQ-038): a false positive here is
    worse than a miss, because a wrong fact is more damaging than a missing one.

    Seeded from `worker/forms/seed/*.yaml` via `python -m api.cli seed-forms`,
    then editable in the app, so the registry is data rather than code.
    """

    __tablename__ = "known_form"

    id: Mapped[uuid.UUID] = uuid_pk()
    # Stable identifier used by seeds and tests: DD-214, W-2, DEED, …
    code: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    match_rules: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    # Typed extractors (separation date, character of service) are Phase 3,
    # T-3.15. The column exists now so the seed format does not have to change.
    field_extractors: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    enabled: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.true()
    )
    created_at: Mapped[datetime] = created_at()
