"""Command line entry points for the trust operations (Phase 6).

These exist so integrity checks, backups and exports can run from cron, from a
Makefile, or from a shell on a box whose web UI is not reachable — which is
exactly the situation in which you most want them. The HTTP endpoints and these
commands call the same functions.

    python -m api.export.cli integrity
    python -m api.export.cli backup [--force]
    python -m api.export.cli export [--name NAME]
    python -m api.export.cli mirror
"""

import argparse
import asyncio
import json
import logging
import sys

import sqlalchemy as sa

from api.db.models import Library
from api.db.session import SessionFactory
from api.export import archive_export, backup, integrity, mirror


async def _library_ids() -> list:
    async with SessionFactory() as session:
        return list((await session.execute(sa.select(Library.id))).scalars().all())


async def _integrity() -> int:
    async with SessionFactory() as session:
        report = await integrity.check(session)
    print(json.dumps(report.as_dict(), indent=2))
    # A non-zero exit so cron and CI notice, rather than a healthy-looking log.
    return 0 if report.healthy else 1


async def _backup(force: bool) -> int:
    async with SessionFactory() as session:
        report = await integrity.check(session)
    try:
        result = await asyncio.to_thread(backup.run_backup, report, allow_unhealthy=force)
    except RuntimeError as error:
        print(f"backup refused: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result.manifest, indent=2))
    return 0


async def _export(name: str) -> int:
    async with SessionFactory() as session:
        result = await archive_export.full_export(session, await _library_ids(), name=name)
    print(f"{result.document_count} documents in {result.file_count} files → {result.path}")
    if result.missing_blobs:
        print(f"{len(result.missing_blobs)} originals missing", file=sys.stderr)
        return 1
    return 0


async def _mirror() -> int:
    async with SessionFactory() as session:
        result = await mirror.rebuild(session, await _library_ids())
    print(
        f"{result.linked} linked, {result.copied} copied, {result.bundles} bundles, "
        f"{result.removed} stale removed → {result.root}"
    )
    return 0 if result.missing == 0 else 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="api.export.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("integrity", help="re-hash every original")
    backup_parser = sub.add_parser("backup", help="integrity, then database, then blobs")
    backup_parser.add_argument(
        "--force", action="store_true", help="back up even if integrity is failing"
    )
    export_parser = sub.add_parser("export", help="full export; works without Bindery")
    export_parser.add_argument("--name", default="bindery-export")
    sub.add_parser("mirror", help="rebuild the browsable folder tree")

    args = parser.parse_args()
    match args.command:
        case "integrity":
            return asyncio.run(_integrity())
        case "backup":
            return asyncio.run(_backup(args.force))
        case "export":
            return asyncio.run(_export(args.name))
        case "mirror":
            return asyncio.run(_mirror())
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
