import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, created_at, uuid_pk


class RelayRegistration(Base):
    """A device's push registration (BND-T-23.4, the D3 App contract's push).

    The relay to post to and the key the device made for this connection: every notification is
    sealed to it, so neither the relay nor Apple can read one; Bindery never holds an APNs token.

    It belongs to what signed the device in: a native session — `session_id` is the refresh-token
    row it was registered with, and the chain from it is followed to the live end — or, for a D3
    Auth token, which has no session here, the OIDC identity. When that ends, the registration is
    forgotten. **Forgotten, not deleted** (invariant 3): `forgotten_at` is set, and nothing is sent
    to it again.
    """

    __tablename__ = "relay_registration"
    __table_args__ = (
        sa.Index(
            "uq_relay_registration_live",
            "relay_url",
            "registration",
            unique=True,
            postgresql_where=sa.text("forgotten_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("refresh_token.id")
    )
    identity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("oidc_identity.id")
    )
    # P-256, X9.63 uncompressed, 65 bytes.
    device_public_key: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
    relay_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    registration: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # The relay's send key, Fernet-encrypted like every other secret here: whoever holds it can
    # push to the device.
    send_key_sealed: Mapped[str] = mapped_column(sa.Text, nullable=False)
    categories: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), nullable=False, default=list)
    created_at: Mapped[datetime] = created_at()
    forgotten_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
