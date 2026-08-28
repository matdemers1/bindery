"""Test fixtures.

The suite runs against a real PostgreSQL database (`bindery_test`) and applies
the real migrations, so migration 0001 is exercised on every run rather than
being shadowed by `create_all`.

    docker compose -f infra/docker-compose.yml --profile test run --rm test
"""

import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from alembic import command
from api.auth.passwords import hash_password
from api.db.enums import LibraryKind, MembershipRole
from api.db.models import AppUser, Library, Membership
from api.db.session import SessionFactory, engine
from api.main import app

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    """Rebuild the test schema from scratch, through Alembic."""
    import asyncio

    async def reset() -> None:
        async with engine.begin() as connection:
            await connection.execute(sa.text("DROP SCHEMA IF EXISTS public CASCADE"))
            await connection.execute(sa.text("CREATE SCHEMA public"))
        await engine.dispose()

    asyncio.run(reset())
    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    # https so the Secure cookie flag is exercised rather than worked around.
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        yield client


@pytest.fixture
async def user_factory(session: AsyncSession):
    async def make(
        email: str | None = None,
        *,
        library_name: str = "Test Library",
        role: MembershipRole = MembershipRole.OWNER,
        library: Library | None = None,
    ) -> tuple[AppUser, Library]:
        user = AppUser(
            email=email or f"user-{uuid.uuid4().hex[:8]}@example.test",
            password_hash=hash_password(PASSWORD),
        )
        session.add(user)
        if library is None:
            library = Library(name=library_name, kind=LibraryKind.PERSONAL)
            session.add(library)
        await session.flush()
        session.add(Membership(user_id=user.id, library_id=library.id, role=role))
        await session.commit()
        return user, library

    return make


@pytest.fixture
async def signed_in(client: AsyncClient, user_factory):
    """A logged-in client, plus the user and library it can write to."""

    async def sign_in(**kwargs) -> tuple[AppUser, Library]:
        user, library = await user_factory(**kwargs)
        response = await client.post(
            "/api/auth/login", json={"email": user.email, "password": PASSWORD}
        )
        assert response.status_code == 200, response.text
        return user, library

    return sign_in


# ---------------------------------------------------------------------------
# Module-scoped fixtures for the seeded performance suite. A 100K-page index is
# far too expensive to build per test.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
async def session_module() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session


@pytest.fixture(scope="module")
async def library_module(session_module: AsyncSession) -> Library:
    library = Library(name="Performance Seed", kind=LibraryKind.PERSONAL)
    session_module.add(library)
    await session_module.commit()
    return library
