"""Alembic environment.

The URL comes from application settings rather than alembic.ini, so there is one
place secrets live and a password containing '%' cannot break ConfigParser
interpolation.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from api.config import get_settings
from api.db.base import Base

# Registers every table on Base.metadata. Do not remove.
import api.db.models  # noqa: F401  isort:skip

config = context.config
if config.config_file_name is not None:
    # `disable_existing_loggers` defaults to True, which switches off every
    # logger that already exists — including all of `bindery.*` when migrations
    # are run in-process. Anything that ran alembic and then kept going would
    # carry on working in complete silence.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(get_settings().database_url, poolclass=None)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
