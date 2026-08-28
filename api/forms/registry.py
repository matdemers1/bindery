"""Loading the known-form registry.

Seed definitions live in `api/forms/seed/*.yaml` and are loaded into the
`known_form` table by `python -m api.cli seed-forms`. Files are the *source*;
the table is the *registry*, so rules stay editable in the app without a deploy.

Lives in `api/` rather than `worker/` because both processes match forms: the
segment stage does it during the pipeline, and the segments endpoint does it
again after a human moves a boundary.
"""

import uuid
from pathlib import Path
from typing import Any

import sqlalchemy as sa
import yaml
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import Document, KnownForm, Page
from api.forms.matcher import FormMatch, best_match

SEED_DIR = Path(__file__).parent / "seed"


def load_seed_definitions() -> list[dict[str, Any]]:
    definitions = []
    for path in sorted(SEED_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        if not data or "code" not in data:
            raise ValueError(f"{path.name} has no `code`")
        definitions.append(data)
    return definitions


async def seed(session: AsyncSession) -> tuple[int, int]:
    """Upsert every seed definition. Returns (inserted, updated).

    Idempotent, and safe to re-run after editing a rule — matching by `code`
    means a form's identity survives a rename.
    """
    inserted = updated = 0
    for definition in load_seed_definitions():
        existing = (
            await session.execute(
                sa.select(KnownForm.id).where(KnownForm.code == definition["code"])
            )
        ).scalar_one_or_none()

        await session.execute(
            insert(KnownForm)
            .values(
                id=uuid.uuid4(),
                code=definition["code"],
                name=definition["name"],
                description=definition.get("description"),
                match_rules=definition.get("match_rules") or {},
                field_extractors=definition.get("field_extractors") or {},
            )
            .on_conflict_do_update(
                index_elements=[KnownForm.code],
                set_={
                    "name": definition["name"],
                    "description": definition.get("description"),
                    "match_rules": definition.get("match_rules") or {},
                    "field_extractors": definition.get("field_extractors") or {},
                },
            )
        )
        if existing:
            updated += 1
        else:
            inserted += 1
    return inserted, updated


async def active_registry(session: AsyncSession) -> list[tuple[str, dict[str, Any]]]:
    rows = (
        await session.execute(
            sa.select(KnownForm.code, KnownForm.match_rules)
            .where(KnownForm.enabled.is_(True))
            .order_by(KnownForm.code)
        )
    ).all()
    return [(code, rules) for code, rules in rows]


async def _page_text(
    session: AsyncSession, source_file_id: uuid.UUID, start: int, end: int
) -> list[str]:
    rows = (
        await session.execute(
            sa.select(Page.text)
            .where(
                Page.source_file_id == source_file_id,
                Page.page_number >= start,
                Page.page_number <= end,
            )
            .order_by(Page.page_number)
        )
    ).scalars().all()
    return [text or "" for text in rows]


async def match_documents(
    session: AsyncSession, documents: list[Document]
) -> dict[uuid.UUID, FormMatch | None]:
    """Match each document against the registry and set `known_form_id`.

    A document that no longer matches has its form cleared — a boundary moved by
    hand can turn a DD-214 into a fragment, and a stale fact is worse than none.
    """
    registry = await active_registry(session)
    results: dict[uuid.UUID, FormMatch | None] = {}
    if not registry:
        return results

    codes = {
        code: form_id
        for form_id, code in (
            await session.execute(sa.select(KnownForm.id, KnownForm.code))
        ).all()
    }

    for document in documents:
        pages = await _page_text(
            session, document.source_file_id, document.page_start, document.page_end
        )
        found = best_match(registry, pages)
        results[document.id] = found
        document.known_form_id = codes.get(found.code) if found else None

    await session.flush()
    return results
