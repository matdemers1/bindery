"""The two things the sidebar draws, and nothing else.

`Shell` wraps every screen, so its badges are the most frequently requested read
in the application — and they follow the `jobs` topic, which the worker
publishes on every state change of every stage of every file. They were answered
by the two most expensive reads there are: the whole health panel, and the first
page of the review queue with twenty-five serialised documents attached. Two
numbers were taken out of that and the rest was thrown away, four times a second
while an import ran.

A separate route rather than a cheaper mode on `/api/health/panel`, so the
expensive read stays honest about what it costs. The Trust screen wants the
panel and is allowed to wait for it; a badge is not.
"""

import sqlalchemy as sa
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api import health_panel
from api.auth.dependencies import current_user
from api.db import repository
from api.db.enums import ReviewState
from api.db.models import AppUser, Document
from api.db.session import get_session
from api.segments import live
from api.vault import boundary as vault_boundary

router = APIRouter(tags=["health"])


class BadgeOut(BaseModel):
    """Deliberately two fields.

    Anything added here is something the sidebar will fetch on every job
    transition, so a new field is a decision about load rather than a
    convenience.
    """

    review_total: int
    healthy: bool


@router.get("/health/badge", response_model=BadgeOut)
async def badge(
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
) -> BadgeOut:
    """The review count and the warning light, in two indexed counts.

    Both halves have to keep agreeing with the screens they point at: the
    number must match what `/api/review` will show when it is opened, and the
    light must match what `/api/health/panel` will say when it is opened. A
    badge that disagrees with the screen behind it is worse than no badge — it
    is the review badge claiming work you had already accepted, which is the
    failure the live-events seam was built to end.
    """
    library_ids = await repository.visible_library_ids(session, user.id)
    if not library_ids:
        # Zero rather than a refusal, matching the review queue. Somebody with
        # no membership yet has nothing waiting and nothing wrong.
        return BadgeOut(review_total=0, healthy=True)

    review_total = (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(Document)
            .where(
                Document.library_id.in_(library_ids),
                live(),
                Document.review_state == ReviewState.NEEDS_REVIEW.value,
                # The queue excludes the backlog by default (R-03), so counting
                # it here would light a badge for a screen that will not show
                # the work it is counting.
                Document.is_backlog.is_(False),
                vault_boundary.document_clause(user.id),
            )
        )
    ).scalar_one()

    return BadgeOut(
        review_total=review_total,
        healthy=await health_panel.badge_healthy(session, library_ids),
    )
