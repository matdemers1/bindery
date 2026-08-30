"""What is actually running (T-11.5 to T-11.7, REQ-152 to REQ-154).

"Which code is this?" is the first question of every production problem, and a
version somebody has to remember to bump is a version that is wrong. So these
come from the build: CI passes the commit as a build argument, the Dockerfile
stamps it into the environment, and nothing in the repository claims a version
number at all.

The interesting part is not the api's own version — that one is easy, it is the
process answering the request. It is the **worker's**, because the worker is a
separate image that is pulled separately and can be a different commit without
anything looking wrong: the queue drains, the screens render, and a stage
quietly behaves like last week. So the worker records its own build on startup
and on every health pass, and the api compares.

The schema revision is here for the same reason. Migrations are applied
explicitly and never on boot (REQ-114), which is right, and which means the
gap between "the code expects 0018" and "the database is at 0017" is a real
state the system can be in — and the failure it produces is an obscure column
error somewhere far away, hours later.
"""

import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import ServiceHeartbeat

log = logging.getLogger("bindery.version")

# A worker that has not checked in for this long is reported as absent rather
# than as whatever version it last claimed. Comfortably longer than the health
# pass interval, so an ordinary slow cycle does not read as a dead worker.
STALE_AFTER = timedelta(minutes=10)


@dataclass(frozen=True)
class Build:
    commit: str
    built_at: str
    ref: str

    @property
    def short(self) -> str:
        return self.commit[:7] if self.commit != "unknown" else "unknown"


def build_of_this_process() -> Build:
    return Build(
        commit=os.environ.get("BINDERY_COMMIT", "unknown"),
        built_at=os.environ.get("BINDERY_BUILT_AT", "unknown"),
        ref=os.environ.get("BINDERY_REF", "unknown"),
    )


async def announce(session: AsyncSession, service: str) -> None:
    """Record that this service, at this build, is alive.

    Upsert rather than insert: one row per service, so the table answers "what
    is running" rather than accumulating a history nobody reads.
    """
    build = build_of_this_process()
    now = datetime.now(UTC)
    await session.execute(
        sa.dialects.postgresql.insert(ServiceHeartbeat)
        .values(
            service=service,
            commit=build.commit,
            built_at=build.built_at,
            ref=build.ref,
            last_seen_at=now,
        )
        .on_conflict_do_update(
            index_elements=[ServiceHeartbeat.service],
            set_={
                "commit": build.commit,
                "built_at": build.built_at,
                "ref": build.ref,
                "last_seen_at": now,
            },
        )
    )


async def schema_state(session: AsyncSession) -> dict:
    """The revision the database is at, and the one the code expects.

    The expected head is read from the migration files rather than hard-coded,
    so this cannot drift from the thing it is describing.
    """
    applied = (
        await session.execute(sa.text("select version_num from alembic_version"))
    ).scalar_one_or_none()

    expected = _head_revision()
    return {
        "applied": applied,
        "expected": expected,
        # None when the expected head cannot be determined — the api image ships
        # `alembic/`, but saying "in sync" because we could not check would be
        # the worst of the three answers.
        "in_sync": None if expected is None else applied == expected,
    }


def _head_revision() -> str | None:
    """The newest revision in `alembic/versions`, by walking `down_revision`."""
    from pathlib import Path

    directory = Path(__file__).resolve().parent.parent / "alembic" / "versions"
    if not directory.is_dir():
        return None

    revisions: dict[str, str | None] = {}
    for file in directory.glob("*.py"):
        text = file.read_text()
        revision = _assigned(text, "revision")
        down = _assigned(text, "down_revision")
        if revision:
            revisions[revision] = down

    if not revisions:
        return None
    # The head is the revision nothing points down at.
    pointed_at = {down for down in revisions.values() if down}
    heads = [rev for rev in revisions if rev not in pointed_at]
    if len(heads) != 1:
        # Two heads means an un-merged branch, which is a real condition worth
        # reporting rather than picking one of them.
        log.warning("alembic has %s heads: %s", len(heads), heads)
        return None
    return heads[0]


# The annotation varies across the migration files — `str | None` in most,
# `str | Sequence[str] | None` in the ones alembic generated — so the value is
# read past whatever annotation is there rather than after a fixed prefix. A
# parser keyed on one spelling silently reported five heads, which is exactly
# the confidently-wrong answer this module exists to avoid.
#
# `[^=\n]` and not `[^=]`: a negated character class matches newlines, so the
# annotation part happily ran from the docstring's `Revises:` line down to the
# `=` on the real assignment several lines later — and then reported the name as
# "Revises". Every revision parsed as None and they all collapsed into one key.
_ASSIGNMENT = re.compile(
    r"^(?P<name>\w+)\s*(?::[^=\n]*)?=\s*(?P<quote>[\"'])(?P<value>[^\"']*)(?P=quote)",
    re.M,
)


def _assigned(text: str, name: str) -> str | None:
    for match in _ASSIGNMENT.finditer(text):
        if match.group("name") == name:
            return match.group("value")
    return None


async def report(session: AsyncSession) -> dict:
    """Everything the UI needs to say what is running, and whether it agrees."""
    here = build_of_this_process()
    now = datetime.now(UTC)

    rows = (await session.execute(sa.select(ServiceHeartbeat))).scalars().all()
    services = {
        row.service: {
            "commit": row.commit,
            "short": row.commit[:7] if row.commit != "unknown" else "unknown",
            "built_at": row.built_at,
            "ref": row.ref,
            "last_seen_at": row.last_seen_at.isoformat(),
            "stale": (now - row.last_seen_at) > STALE_AFTER,
        }
        for row in rows
    }
    services["api"] = {
        "commit": here.commit,
        "short": here.short,
        "built_at": here.built_at,
        "ref": here.ref,
        "last_seen_at": now.isoformat(),
        "stale": False,
    }

    # A mismatch only counts among services that have actually reported a build.
    # `unknown` means a locally-built image, which is normal in development and
    # would otherwise show a permanent warning nobody reads.
    known = {
        name: info["commit"]
        for name, info in services.items()
        if info["commit"] != "unknown" and not info["stale"]
    }
    return {
        "services": services,
        "mismatch": len(set(known.values())) > 1,
        "schema": await schema_state(session),
    }
