import sqlalchemy as sa
from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from api import version
from api.db.session import get_session
from api.schemas import HealthOut

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
async def health(response: Response, session: AsyncSession = Depends(get_session)) -> HealthOut:
    """Liveness plus a real database round-trip.

    Unauthenticated on purpose: it reveals nothing, and the container healthcheck
    calls it. Returns 503 when the database is unreachable, so an api container
    that cannot serve is not reported as healthy.

    `schema` is the database's current alembic revision (`alembic_version.version_num`),
    null when it cannot be read. Shipyard runs `alembic upgrade head` as a one-shot
    before swapping to the new image, so once the swap completes this must equal the
    new image's `dev.d3cloud.shipyard.schema` label (SHP-D-019/022).
    """
    schema: str | None = None
    try:
        await session.execute(sa.text("SELECT 1"))
        database = "ok"
        try:
            schema = (
                await session.execute(sa.text("select version_num from alembic_version"))
            ).scalar_one_or_none()
        except Exception:
            schema = None
    except Exception:
        database = "unavailable"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthOut(
        status="ok" if database == "ok" else "degraded",
        database=database,
        version=version.build_of_this_process().short,
        schema=schema,
    )


@router.get("/version")
async def read_version(session: AsyncSession = Depends(get_session)) -> dict:
    """What is running, across every service, and whether they agree.

    Unauthenticated on purpose, and it carries nothing that is not already
    inferable from the served bundle's asset hashes. Being able to check what a
    deployment actually landed *without* first signing in is the point — the
    times you need this most are the times signing in is what is broken.
    """
    return await version.report(session)
