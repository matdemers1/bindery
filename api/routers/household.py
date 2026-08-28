"""Libraries, members and moves (Phase 7).

The library is the access boundary (ADR-005), so this is the screen where the
boundary is actually drawn: who is in which library, with what authority, and
what to do about a document that landed in the wrong one.

Every route here goes through `Scope`. None of them builds its own library
filter — that is the property the leak suite exists to keep true.
"""

import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from api import moves
from api.audit import record
from api.auth.dependencies import current_scope, current_user
from api.db.enums import ActorType, LibraryKind, MembershipRole
from api.db.models import AppUser, Library, Membership, SourceFile
from api.db.scope import Scope
from api.db.session import get_session
from api.schemas import (
    LibraryCreateIn,
    LibraryDetailOut,
    MemberOut,
    MembershipIn,
    MovePlanOut,
    MoveRequestIn,
)

router = APIRouter(tags=["household"])


async def _library(session: AsyncSession, scope: Scope, library_id: uuid.UUID) -> Library:
    scope.require_visible(library_id)
    library = await session.get(Library, library_id)
    if library is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return library


# --------------------------------------------------------------------------
# T-7.7 — the libraries screen
# --------------------------------------------------------------------------


@router.get("/household/libraries", response_model=list[LibraryDetailOut])
async def list_libraries(
    session: AsyncSession = Depends(get_session),
    scope: Scope = Depends(current_scope),
) -> list[LibraryDetailOut]:
    """Every library the caller is in, with its members and their roles."""
    rows = (
        await session.execute(
            sa.select(Library, Membership, AppUser)
            .join(Membership, Membership.library_id == Library.id)
            .join(AppUser, AppUser.id == Membership.user_id)
            .where(Library.id.in_(scope.visible))
            .order_by(Library.name, AppUser.email)
        )
    ).all()

    libraries: dict[uuid.UUID, LibraryDetailOut] = {}
    for library, membership, member in rows:
        entry = libraries.get(library.id)
        if entry is None:
            entry = LibraryDetailOut(
                id=library.id,
                name=library.name,
                kind=str(library.kind),
                # What *you* may do here, which is the question the screen is
                # really answering.
                your_role=str(scope.roles[library.id]),
                members=[],
            )
            libraries[library.id] = entry
        entry.members.append(
            MemberOut(
                user_id=member.id,
                email=member.email,
                display_name=member.display_name,
                role=str(membership.role),
            )
        )
    return list(libraries.values())


@router.post("/household/libraries", response_model=LibraryDetailOut, status_code=201)
async def create_library(
    body: LibraryCreateIn,
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
) -> LibraryDetailOut:
    """Create a library. The creator owns it; there is no other way in."""
    library = Library(name=body.name.strip(), kind=LibraryKind(body.kind))
    session.add(library)
    await session.flush()
    session.add(
        Membership(user_id=user.id, library_id=library.id, role=MembershipRole.OWNER)
    )
    await record(
        session,
        entity_type="library",
        entity_id=library.id,
        action="library_created",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        after={"name": library.name, "kind": str(library.kind)},
    )
    await session.commit()
    return LibraryDetailOut(
        id=library.id, name=library.name, kind=str(library.kind),
        your_role=str(MembershipRole.OWNER),
        members=[
            MemberOut(
                user_id=user.id, email=user.email, display_name=user.display_name,
                role=str(MembershipRole.OWNER),
            )
        ],
    )


@router.put("/household/libraries/{library_id}/members", response_model=MemberOut)
async def set_member_role(
    library_id: uuid.UUID,
    body: MembershipIn,
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
    scope: Scope = Depends(current_scope),
) -> MemberOut:
    """Add someone to a library, or change what they may do in it.

    Owner-only. A contributor who could grant themselves ownership is not a
    contributor, and a reader who could add a reader has write access to the
    membership table if nothing else.
    """
    await _library(session, scope, library_id)
    if not scope.is_owner(library_id):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "only an owner can change who is in a library"
        )

    member = (
        await session.execute(sa.select(AppUser).where(AppUser.email == body.email.lower()))
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")

    role = MembershipRole(body.role)
    existing = (
        await session.execute(
            sa.select(Membership).where(
                Membership.user_id == member.id, Membership.library_id == library_id
            )
        )
    ).scalar_one_or_none()

    before = str(existing.role) if existing else None
    if existing is None:
        session.add(Membership(user_id=member.id, library_id=library_id, role=role))
    else:
        if (
            existing.role == MembershipRole.OWNER
            and role != MembershipRole.OWNER
            and not await _has_another_owner(session, library_id, member.id)
        ):
            # A library with no owner is a library nobody can ever fix.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "that is the only owner; promote someone else first",
            )
        existing.role = role

    await record(
        session,
        entity_type="library",
        entity_id=library_id,
        action="membership_changed",
        actor_type=ActorType.HUMAN,
        actor_id=user.id,
        before={"user": member.email, "role": before},
        after={"user": member.email, "role": str(role)},
    )
    await session.commit()
    return MemberOut(
        user_id=member.id, email=member.email,
        display_name=member.display_name, role=str(role),
    )


async def _has_another_owner(
    session: AsyncSession, library_id: uuid.UUID, excluding: uuid.UUID
) -> bool:
    count = await session.scalar(
        sa.select(sa.func.count())
        .select_from(Membership)
        .where(
            Membership.library_id == library_id,
            Membership.role == MembershipRole.OWNER,
            Membership.user_id != excluding,
        )
    )
    return bool(count)


# --------------------------------------------------------------------------
# T-7.3 — audited moves (REQ-102)
# --------------------------------------------------------------------------


async def _movable(
    session: AsyncSession, scope: Scope, source_file_id: uuid.UUID
) -> SourceFile:
    source_file = await session.get(SourceFile, source_file_id)
    # 404 whether it is missing or simply not theirs: a 403 would confirm that
    # a file with that id exists somewhere in the household.
    if source_file is None or source_file.library_id not in scope.visible:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    scope.require_write(source_file.library_id)
    return source_file


@router.post("/source-files/{source_file_id}/move/preview", response_model=MovePlanOut)
async def preview_move(
    source_file_id: uuid.UUID,
    body: MoveRequestIn,
    session: AsyncSession = Depends(get_session),
    scope: Scope = Depends(current_scope),
) -> MovePlanOut:
    """What the move would do — including what it would cost you."""
    source_file = await _movable(session, scope, source_file_id)
    scope.require_write(body.to_library_id)
    result = await moves.plan(session, source_file, body.to_library_id)
    return MovePlanOut(**result.as_dict(), loses_metadata=result.loses_metadata)


@router.post("/source-files/{source_file_id}/move", response_model=MovePlanOut)
async def move_file(
    source_file_id: uuid.UUID,
    body: MoveRequestIn,
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
    scope: Scope = Depends(current_scope),
) -> MovePlanOut:
    """Move a file, and every document in it, into another library.

    Write access is required at **both** ends: moving out of a library is a
    change to it, and moving into one is a change to that. A reader on either
    side cannot do this.
    """
    source_file = await _movable(session, scope, source_file_id)
    scope.require_write(body.to_library_id)
    result = await moves.move(session, source_file, body.to_library_id, actor_id=user.id)
    await session.commit()
    return MovePlanOut(**result.as_dict(), loses_metadata=result.loses_metadata)
