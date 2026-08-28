"""Declarative base and metadata conventions."""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Explicit constraint names, so migrations can always refer to them by name
# rather than by whatever the backend happened to generate.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)


def pg_enum(enum_cls: type, name: str) -> sa.Enum:
    """A native PostgreSQL enum storing member *values*, not member names.

    SQLAlchemy defaults to persisting names ("PERSONAL"), which would not match
    the lowercase type created by the migration.
    """
    return sa.Enum(
        enum_cls, name=name, values_callable=lambda enum: [member.value for member in enum]
    )


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def created_at() -> Mapped[datetime]:
    return mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
