import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base


class Setting(Base):
    """A runtime-editable configuration value.

    Anything here overrides the environment, so a value set in the UI wins over
    one baked into the compose file. That ordering is deliberate: the UI is the
    surface a person actually reaches for, and a setting that silently loses to
    an env var is a setting that looks broken.
    """

    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    value: Mapped[str | None] = mapped_column(sa.Text)
    # Encrypted at rest, and never returned to a client.
    is_secret: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("app_user.id")
    )
