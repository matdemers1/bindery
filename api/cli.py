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
from api.auth.passwords import WeakPassword, hash_password, validate_password
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


async def _reprocess(prompt_version: str, dry_run: bool) -> None:
    """Re-classify every document whose latest classification is on a version.

    "Reprocess everything still on v2" is the operation replayable stages exist
    to make affordable: no re-OCR, no re-segmentation, just the one stage whose
    inputs changed.
    """
    from api import queue
    from api.db.enums import JobStage
    from api.db.models import Classification

    async with SessionFactory() as session:
        newest = (
            sa.select(
                Classification.document_id,
                sa.func.max(Classification.created_at).label("latest"),
            )
            .group_by(Classification.document_id)
            .subquery()
        )
        document_ids = (
            await session.execute(
                sa.select(Classification.document_id)
                .join(
                    newest,
                    sa.and_(
                        newest.c.document_id == Classification.document_id,
                        newest.c.latest == Classification.created_at,
                    ),
                )
                .where(Classification.prompt_version == prompt_version)
            )
        ).scalars().all()

        if dry_run:
            print(f"{len(document_ids)} document(s) are on prompt version {prompt_version}")
            return

        queued = 0
        for document_id in document_ids:
            if await queue.requeue_stage(session, JobStage.CLASSIFY, document_id=document_id):
                queued += 1
        await session.commit()
        print(f"queued {queued} re-classification(s) for prompt version {prompt_version}")


async def _create_user(email: str, password: str, library_name: str, kind: str) -> None:
    email = email.strip().lower()
    async with SessionFactory() as session:
        existing = await session.execute(sa.select(AppUser).where(AppUser.email == email))
        if existing.scalar_one_or_none() is not None:
            print(f"user {email} already exists", file=sys.stderr)
            raise SystemExit(1)

        # Enforced here as well as at the API, because this is the path that
        # creates the first account — the one with nothing above it.
        try:
            validate_password(password, email=email)
        except WeakPassword as weak:
            print(f"refusing that password: {weak}", file=sys.stderr)
            raise SystemExit(1) from weak

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



async def _lifecycle_check() -> int:
    """Audit the live bucket's lifecycle rules (T-13.9, REQ-166, R-21).

    The test suite audits the configuration checked into `infra/aws/`, which is
    what gets deployed. This audits what the bucket *has*, which is a different
    question the moment somebody edits a rule in the console — and the mistake
    being guarded against is invisible from inside the application, because
    Bindery holds no delete permission and would neither cause nor be told about
    a rule that deleted the archive.

    Exits non-zero on any finding, so it is safe in cron without a wrapper that
    has to interpret log output.
    """
    from api import offsite
    from api.db.session import SessionFactory

    async with SessionFactory() as session:
        config = await offsite.config_from_settings(session)
    if not config.complete:
        print("offsite replication is not configured — nothing to audit")
        return 0

    client = offsite.make_client(config)
    rules = await asyncio.to_thread(offsite.live_lifecycle_rules, client, config)
    if not rules:
        print(f"{config.bucket}: NO lifecycle rules at all — dumps would accumulate "
              "forever", file=sys.stderr)
        return 1

    findings = offsite.audit_lifecycle(rules)
    for rule in rules:
        prefix = offsite._rule_prefix(rule) or "(everything)"
        expires = offsite._expires_objects(rule)
        print(f"  {rule.get('ID', '(unnamed)'):32} {prefix:24} "
              f"{'expires objects' if expires else 'safe'}")

    if findings:
        print(f"\n{len(findings)} finding(s) in {config.bucket}:", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        return 1
    print(f"\n{config.bucket}: rules are safe, and cover every key this build writes")
    return 0



async def _offsite_fetch(into: str, kind: str | None) -> int:
    """Pull the newest offsite generation's dump down for a restore drill.

    Only the dump. The blobs come afterwards, once the restored database has
    said which ones it actually references — which is both cheaper than pulling
    the whole pool and closer to what a real recovery does.
    """
    from pathlib import Path as _Path

    from api import offsite
    from api.db.session import SessionFactory

    async with SessionFactory() as session:
        config = await offsite.config_from_settings(session)
    if not config.complete:
        print("offsite replication is not configured", file=sys.stderr)
        return 1

    client = offsite.make_client(config)
    wanted = offsite.Kind(kind) if kind else None
    key = await asyncio.to_thread(offsite.newest_dump, client, config, wanted)
    if key is None:
        print(f"no dump found in {config.bucket}", file=sys.stderr)
        return 1

    destination = _Path(into)
    size = await asyncio.to_thread(
        offsite.download, client, config, key, destination / "bindery.dump"
    )
    print(f"{key} -> {destination / 'bindery.dump'} ({size} bytes)")

    manifest_key = offsite.manifest_object_key(key)
    try:
        await asyncio.to_thread(
            offsite.download, client, config, manifest_key, destination / "manifest.json"
        )
        print(f"{manifest_key} -> manifest.json")
    except Exception as error:
        # The dump is what a restore needs; the manifest only describes it.
        print(f"  (no manifest: {type(error).__name__})", file=sys.stderr)
    return 0


async def _offsite_blobs(into: str, sha_file: str) -> int:
    """Fetch the named originals and verify each against its content address.

    The local drill asks whether a file is present in the backup directory.
    This asks the stronger question the offsite copy makes possible: does the
    object come back, and are the bytes the ones the database asked for. A blob
    that is present but wrong is the failure a presence check cannot see.
    """
    from pathlib import Path as _Path

    from api import offsite
    from api.db.session import SessionFactory

    async with SessionFactory() as session:
        config = await offsite.config_from_settings(session)
    if not config.complete:
        print("offsite replication is not configured", file=sys.stderr)
        return 1

    shas = [line.strip() for line in _Path(sha_file).read_text().splitlines() if line.strip()]
    client = offsite.make_client(config)
    result = await asyncio.to_thread(
        offsite.fetch_blobs, client, config, shas, _Path(into)
    )

    print(f"  {result.fetched}/{len(shas)} originals downloaded and hash-verified "
          f"({result.bytes_read} bytes)")
    for sha in result.missing:
        print(f"  MISSING FROM BUCKET {sha}", file=sys.stderr)
    for sha in result.corrupt:
        print(f"  CORRUPT IN BUCKET   {sha}", file=sys.stderr)
    return 0 if result.ok else 1


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
    fetch = sub.add_parser(
        "offsite-fetch", help="download the newest offsite dump for a restore drill"
    )
    fetch.add_argument("--into", required=True)
    fetch.add_argument("--kind", choices=["daily", "weekly"])

    blobs = sub.add_parser(
        "offsite-blobs", help="download and hash-verify the named originals from S3"
    )
    blobs.add_argument("--into", required=True)
    blobs.add_argument("--from-file", dest="sha_file", required=True)

    sub.add_parser(
        "lifecycle-check",
        help="audit the offsite bucket's lifecycle rules against what this build writes",
    )

    enqueue = sub.add_parser("enqueue-stage", help="re-run a pipeline stage over every file")
    enqueue.add_argument("stage")

    reprocess = sub.add_parser(
        "reprocess", help="re-classify documents left on an older prompt version"
    )
    reprocess.add_argument("--prompt-version", required=True)
    reprocess.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    if args.command == "offsite-fetch":
        raise SystemExit(asyncio.run(_offsite_fetch(args.into, args.kind)))
    if args.command == "offsite-blobs":
        raise SystemExit(asyncio.run(_offsite_blobs(args.into, args.sha_file)))
    if args.command == "lifecycle-check":
        raise SystemExit(asyncio.run(_lifecycle_check()))
    if args.command == "seed-forms":
        asyncio.run(_seed_forms())
        return
    if args.command == "enqueue-stage":
        asyncio.run(_enqueue_stage(args.stage))
        return
    if args.command == "reprocess":
        asyncio.run(_reprocess(args.prompt_version, args.dry_run))
        return

    if args.command == "create-user":
        password = args.password or getpass.getpass("password: ")
        if len(password) < 12:
            print("password must be at least 12 characters", file=sys.stderr)
            raise SystemExit(1)
        asyncio.run(_create_user(args.email, password, args.library, args.kind))


if __name__ == "__main__":
    main()
