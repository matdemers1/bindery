import sqlalchemy as sa
from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.session import get_session
from api.schemas import HealthOut

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
async def health(response: Response, session: AsyncSession = Depends(get_session)) -> HealthOut:
    """Liveness plus a real database round-trip.

    Unauthenticated on purpose: it reveals nothing, and the container healthcheck
    calls it. Returns 503 when the database is unreachable, so an api container
    that cannot serve is not reported as healthy.
    """
    try:
        await session.execute(sa.text("SELECT 1"))
        database = "ok"
    except Exception:
        database = "unavailable"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthOut(
        status="ok" if database == "ok" else "degraded",
        database=database,
        version="0.1.0",
    )
