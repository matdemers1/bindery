"""Administrative commands.

    docker compose -f infra/docker-compose.yml exec api \
        python -m api.cli create-user --email you@example.com --library "Household"

There is no self-service registration: Bindery is a household archive, not a
signup funnel, and the first account is created deliberately.
"""

import argparse
import asyncio
import getpass
import sys

import sqlalchemy as sa

from api.audit import record
from api.auth.passwords import hash_password
from api.db.enums import ActorType, LibraryKind, MembershipRole
from api.db.models import AppUser, Library, Membership
from api.db.session import SessionFactory


async def _seed_forms() -> None:
    from api.forms.registry import seed

    async with SessionFactory() as session:
        inserted, updated = await seed(session)
        await session.commit()
        print(f"known forms: {inserted} inserted, {updated} updated")


async def _enqueue_stage(stage_name: str) -> None:
    """Queue a stage for every processed source file.

    The upgrade path when a new stage is added: rather than backfilling data in a
    migration, re-run the real code path over what is already stored. Existing
    artifacts make it cheap — this is what replayable stages are for.
    """
    from api import queue
    from api.db.enums import JobStage
    from api.db.models import SourceFile

    stage = JobStage(stage_name)
    async with SessionFactory() as session:
        source_files = (
            await session.execute(sa.select(SourceFile.id).order_by(SourceFile.received_at))
        ).scalars().all()
        queued = 0
        for source_file_id in source_files:
            if await queue.enqueue(session, stage, source_file_id=source_file_id):
                queued += 1
        await session.commit()
        print(f"queued {queued} {stage.value} job(s) across {len(source_files)} file(s)")


async def _create_user(email: str, password: str, library_name: str, kind: str) -> None:
    email = email.strip().lower()
    async with SessionFactory() as session:
        existing = await session.execute(sa.select(AppUser).where(AppUser.email == email))
        if existing.scalar_one_or_none() is not None:
            print(f"user {email} already exists", file=sys.stderr)
            raise SystemExit(1)

        user = AppUser(email=email, password_hash=hash_password(password))
        library = Library(name=library_name, kind=LibraryKind(kind))
        session.add_all([user, library])
        await session.flush()

        session.add(
            Membership(user_id=user.id, library_id=library.id, role=MembershipRole.OWNER)
        )
        await record(
            session,
            entity_type="app_user",
            entity_id=user.id,
            action="create",
            actor_type=ActorType.SYSTEM,
            after={"email": email, "library": library_name},
        )
        await session.commit()

        print(f"created user {email}")
        print(f"created library {library_name} ({library.id}) with role owner")


def main() -> None:
    parser = argparse.ArgumentParser(prog="api.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-user", help="create a user and their first library")
    create.add_argument("--email", required=True)
    create.add_argument("--password", help="prompted for if omitted")
    create.add_argument("--library", default="Personal")
    create.add_argument("--kind", default=LibraryKind.PERSONAL.value,
                        choices=[k.value for k in LibraryKind])

    sub.add_parser("seed-forms", help="load api/forms/seed/*.yaml into the registry")

    enqueue = sub.add_parser("enqueue-stage", help="re-run a pipeline stage over every file")
    enqueue.add_argument("stage")

    args = parser.parse_args()
    if args.command == "seed-forms":
        asyncio.run(_seed_forms())
        return
    if args.command == "enqueue-stage":
        asyncio.run(_enqueue_stage(args.stage))
        return

    if args.command == "create-user":
        password = args.password or getpass.getpass("password: ")
        if len(password) < 12:
            print("password must be at least 12 characters", file=sys.stderr)
            raise SystemExit(1)
        asyncio.run(_create_user(args.email, password, args.library, args.kind))


if __name__ == "__main__":
    main()
