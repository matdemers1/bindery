"""Correspondents, assets, shelves, and taxonomy health (Phase 5).

Merge is the operation everything here is shaped around: previewed before
commit, executed in one transaction, recorded as one event, undone in one action.
"""

import logging
import uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from api import ai_ask, entities, settings_store, taxonomy_health, unify
from api.audit import record
from api.auth.dependencies import current_user
from api.db import repository
from api.db.enums import ActorType, AssetKind, TagSource
from api.db.models import (
    AppUser,
    Asset,
    Correspondent,
    CorrespondentAlias,
    Document,
    DocumentAsset,
    DuplicatePair,
    SavedSearch,
    Tag,
)
from api.db.session import get_session
from api.schemas import (
    AssetIn,
    AssetOut,
    AssetTimelineOut,
    CorrespondentOut,
    DuplicatePairOut,
    MergeIn,
    MergePreviewOut,
    SavedSearchIn,
    SavedSearchOut,
    SimilarOut,
    TaxonomyHealthOut,
    UnifyApplyIn,
    UnifyProposalOut,
)
from api.segments import live

log = logging.getLogger("bindery.entities")

router = APIRouter(tags=["entities"])


async def _writable(session: AsyncSession, user: AppUser) -> list[uuid.UUID]:
    ids = await repository.writable_library_ids(session, user.id)
    if not ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no writable libraries")
    return ids


# --------------------------------------------------------------------------
# Correspondents and aliases (T-5.1, T-5.2)
# --------------------------------------------------------------------------


@router.get("/correspondents", response_model=list[CorrespondentOut])
async def list_correspondents(
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list:
    library_ids = await repository.visible_library_ids(session, user.id)
    if not library_ids:
        return []
    rows = (
        await session.execute(
            sa.select(
                Correspondent,
                sa.select(sa.func.count()).select_from(Document)
                .where(Document.correspondent_id == Correspondent.id, live())
                .scalar_subquery().label("document_count"),
            )
            .where(
                Correspondent.library_id.in_(library_ids),
                # Merged-away records are history, not choices.
                Correspondent.merged_at.is_(None),
            )
            .order_by(Correspondent.name)
        )
    ).all()

    out = []
    for correspondent, count in rows:
        aliases = (
            await session.execute(
                sa.select(CorrespondentAlias.alias).where(
                    CorrespondentAlias.correspondent_id == correspondent.id
                )
            )
        ).scalars().all()
        out.append(
            CorrespondentOut(
                id=correspondent.id, name=correspondent.name, kind=correspondent.kind,
                aliases=list(aliases), document_count=count,
            )
        )
    return out


@router.post("/correspondents/{correspondent_id}/aliases", response_model=CorrespondentOut)
async def add_alias(
    correspondent_id: uuid.UUID,
    alias: str = Query(min_length=1),
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> CorrespondentOut:
    library_ids = await _writable(session, user)
    correspondent = (
        await session.execute(
            sa.select(Correspondent).where(
                Correspondent.id == correspondent_id,
                Correspondent.library_id.in_(library_ids),
            )
        )
    ).scalar_one_or_none()
    if correspondent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    await entities.add_alias(session, correspondent, alias)
    await session.commit()
    aliases = (
        await session.execute(
            sa.select(CorrespondentAlias.alias).where(
                CorrespondentAlias.correspondent_id == correspondent_id
            )
        )
    ).scalars().all()
    return CorrespondentOut(
        id=correspondent.id, name=correspondent.name, kind=correspondent.kind,
        aliases=list(aliases), document_count=0,
    )


@router.post("/correspondents/merge/preview", response_model=MergePreviewOut)
async def preview_merge(
    payload: MergeIn,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> MergePreviewOut:
    """What the merge would touch. Writes nothing."""
    await _writable(session, user)
    try:
        preview = await entities.preview_correspondent_merge(
            session, payload.source_id, payload.target_id
        )
    except entities.MergeError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return MergePreviewOut(
        from_name=preview.from_name, into_name=preview.into_name,
        document_count=preview.document_count, alias_count=preview.alias_count,
        documents=preview.documents,
    )


@router.post("/correspondents/merge", response_model=MergePreviewOut)
async def merge_correspondents(
    payload: MergeIn,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> MergePreviewOut:
    await _writable(session, user)
    try:
        preview = await entities.preview_correspondent_merge(
            session, payload.source_id, payload.target_id
        )
        operation_id = await entities.merge_correspondents(
            session, payload.source_id, payload.target_id, actor_id=user.id
        )
    except entities.MergeError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    await session.commit()
    return MergePreviewOut(
        from_name=preview.from_name, into_name=preview.into_name,
        document_count=preview.document_count, alias_count=preview.alias_count,
        documents=preview.documents, operation_id=operation_id,
    )


@router.post("/tags/merge", response_model=MergePreviewOut)
async def merge_tags(
    payload: MergeIn,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> MergePreviewOut:
    """Merge two tags retroactively across every document (REQ-076)."""
    await _writable(session, user)
    source = await session.get(Tag, payload.source_id)
    target = await session.get(Tag, payload.target_id)
    if source is None or target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    try:
        operation_id = await entities.merge_tags(
            session, payload.source_id, payload.target_id, actor_id=user.id
        )
    except entities.MergeError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    await session.commit()
    return MergePreviewOut(
        from_name=source.name, into_name=target.name,
        document_count=0, alias_count=0, documents=[], operation_id=operation_id,
    )


@router.post("/merges/{operation_id}/undo")
async def undo_merge(
    operation_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Reverse a merge — the record and every link row it moved — in one action."""
    await _writable(session, user)
    try:
        restored = await entities.undo_merge(session, operation_id, actor_id=user.id)
    except entities.MergeError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await session.commit()
    return {"restored": restored}


# --------------------------------------------------------------------------
# Assets (T-5.3, T-5.4)
# --------------------------------------------------------------------------


@router.get("/assets", response_model=list[AssetOut])
async def list_assets(
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list:
    library_ids = await repository.visible_library_ids(session, user.id)
    if not library_ids:
        return []
    rows = (
        await session.execute(
            sa.select(
                Asset,
                sa.select(sa.func.count()).select_from(DocumentAsset)
                .where(DocumentAsset.asset_id == Asset.id, DocumentAsset.removed_at.is_(None))
                .scalar_subquery().label("document_count"),
            )
            .where(Asset.library_id.in_(library_ids))
            .order_by(Asset.kind, Asset.name)
        )
    ).all()
    return [
        AssetOut(id=a.id, library_id=a.library_id, kind=a.kind.value, name=a.name,
                 attributes=a.attributes, document_count=count)
        for a, count in rows
    ]


@router.post("/assets", response_model=AssetOut, status_code=status.HTTP_201_CREATED)
async def create_asset(
    payload: AssetIn,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AssetOut:
    if not await repository.can_write_library(session, user.id, payload.library_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no write access to that library")

    asset = Asset(
        library_id=payload.library_id, kind=AssetKind(payload.kind), name=payload.name,
        slug=entities.slugify(payload.name), attributes=payload.attributes,
    )
    session.add(asset)
    await session.flush()
    await record(
        session, entity_type="asset", entity_id=asset.id, action="create",
        actor_type=ActorType.HUMAN, actor_id=user.id,
        after={"name": asset.name, "kind": asset.kind.value},
    )
    await session.commit()
    return AssetOut(id=asset.id, library_id=asset.library_id, kind=asset.kind.value,
                    name=asset.name, attributes=asset.attributes, document_count=0)


@router.post("/assets/{asset_id}/documents/{document_id}")
async def attach(
    asset_id: uuid.UUID,
    document_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    document = await repository.get_document(session, user.id, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    asset = await session.get(Asset, asset_id)
    if asset is None or asset.library_id != document.library_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    await entities.attach_asset(session, document_id, asset_id, TagSource.HUMAN)
    await record(
        session, entity_type="document", entity_id=document_id, action="attach_asset",
        actor_type=ActorType.HUMAN, actor_id=user.id,
        after={"asset_id": str(asset_id), "asset_name": asset.name},
    )
    await session.commit()
    return {"attached": True}


@router.get("/assets/{asset_id}/timeline", response_model=AssetTimelineOut)
async def timeline(
    asset_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AssetTimelineOut:
    """Everything about one asset, in the order it happened (REQ-075)."""
    library_ids = await repository.visible_library_ids(session, user.id)
    asset = await session.get(Asset, asset_id)
    if asset is None or asset.library_id not in library_ids:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return AssetTimelineOut(
        asset=AssetOut(id=asset.id, library_id=asset.library_id, kind=asset.kind.value,
                       name=asset.name, attributes=asset.attributes, document_count=0),
        entries=await entities.asset_timeline(session, asset_id),
    )


# --------------------------------------------------------------------------
# Shelves (T-5.8)
# --------------------------------------------------------------------------


@router.get("/shelves", response_model=list[SavedSearchOut])
async def list_shelves(
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list:
    library_ids = await repository.visible_library_ids(session, user.id)
    if not library_ids:
        return []
    rows = (
        await session.execute(
            sa.select(SavedSearch)
            .where(SavedSearch.library_id.in_(library_ids), SavedSearch.user_id == user.id)
            .order_by(SavedSearch.name)
        )
    ).scalars().all()
    return [SavedSearchOut.model_validate(row) for row in rows]


@router.post("/shelves", response_model=SavedSearchOut, status_code=status.HTTP_201_CREATED)
async def create_shelf(
    payload: SavedSearchIn,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SavedSearchOut:
    """A saved query. It stays current as documents arrive — that is the point."""
    if not await repository.can_write_library(session, user.id, payload.library_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no write access to that library")
    shelf = SavedSearch(
        library_id=payload.library_id, user_id=user.id, name=payload.name,
        query=payload.query, is_packet=payload.is_packet,
    )
    session.add(shelf)
    await session.commit()
    await session.refresh(shelf)
    return SavedSearchOut.model_validate(shelf)


# --------------------------------------------------------------------------
# Taxonomy health, duplicates, similar (T-5.6, T-5.10, T-5.11)
# --------------------------------------------------------------------------


@router.get("/taxonomy/health", response_model=TaxonomyHealthOut)
async def health(
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TaxonomyHealthOut:
    library_ids = await repository.visible_library_ids(session, user.id)
    if not library_ids:
        return TaxonomyHealthOut(
            total_tags=0, used_once=0, unused=0, orphan_ratio=0.0, exceeds_alarm=False,
            near_duplicate_tags=[], near_duplicate_correspondents=[],
        )
    result = await taxonomy_health.report(session, library_ids)
    return TaxonomyHealthOut(**result.__dict__)


@router.get("/documents/{document_id}/similar", response_model=SimilarOut)
async def similar(
    document_id: uuid.UUID,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SimilarOut:
    library_ids = await repository.visible_library_ids(session, user.id)
    if await repository.get_document(session, user.id, document_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return SimilarOut(
        results=await taxonomy_health.find_similar(session, document_id, library_ids)
    )


@router.get("/duplicates", response_model=list[DuplicatePairOut])
async def duplicates(
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list:
    library_ids = await repository.visible_library_ids(session, user.id)
    if not library_ids:
        return []
    a, b = sa.orm.aliased(Document), sa.orm.aliased(Document)
    rows = (
        await session.execute(
            sa.select(DuplicatePair, a.title, b.title)
            .join(a, a.id == DuplicatePair.document_a_id)
            .join(b, b.id == DuplicatePair.document_b_id)
            .where(
                DuplicatePair.library_id.in_(library_ids),
                DuplicatePair.dismissed_at.is_(None),
            )
            .order_by(DuplicatePair.similarity.desc())
        )
    ).all()
    return [
        DuplicatePairOut(
            id=pair.id, document_a_id=pair.document_a_id, document_b_id=pair.document_b_id,
            a_title=a_title, b_title=b_title, similarity=pair.similarity,
        )
        for pair, a_title, b_title in rows
    ]


@router.post("/duplicates/scan")
async def scan_duplicates(
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Find near-duplicates. Records them; resolves nothing."""
    library_ids = await _writable(session, user)
    found = 0
    for library_id in library_ids:
        found += await taxonomy_health.detect_duplicates(session, library_id)
    await session.commit()
    return {"found": found}


# --------------------------------------------------------------------------
# Unifying correspondents (T-8.17)
# --------------------------------------------------------------------------


@router.post("/taxonomy/unify/preview", response_model=UnifyProposalOut)
async def unify_preview(
    kind: str = Query("correspondent", description="correspondent | document_type | tag"),
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
) -> UnifyProposalOut:
    """Ask which entries of this kind are the same thing. Changes nothing.

    Only the names are sent — never document text — so this costs almost
    nothing and nothing about the contents of the archive leaves it.
    """
    if kind not in unify.KINDS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"kind must be one of {', '.join(unify.KINDS)}",
        )
    library_ids = await _writable(session, user)
    key = await settings_store.get(session, settings_store.ANTHROPIC_API_KEY)
    model = await settings_store.get(session, settings_store.BINDERY_MODEL) or "claude-opus-5"
    answerer = ai_ask.ClaudeAnswerer(key or "", model=model) if key else None

    proposal = await unify.propose(session, library_ids, answerer, kind)
    return UnifyProposalOut(**proposal.as_dict())


@router.post("/taxonomy/unify/apply", response_model=MergePreviewOut)
async def unify_apply(
    body: UnifyApplyIn,
    kind: str = Query("correspondent", description="correspondent | document_type | tag"),
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
) -> MergePreviewOut:
    """Apply one proposed group, as ordinary merges.

    Deliberately one group at a time and never straight from the proposal: the
    request names the exact records to merge, so what gets applied is what was
    shown on screen rather than whatever the model would say if asked again.
    Each merge is audited and undoable on its own.
    """
    if kind not in unify.KINDS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"kind must be one of {', '.join(unify.KINDS)}",
        )
    spec = unify.KINDS[kind]
    merge = getattr(entities, spec.merge)

    library_ids = await _writable(session, user)
    target = await session.get(spec.model, body.canonical_id)
    if target is None or target.library_id not in library_ids:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    merged = 0
    for source_id in body.member_ids:
        if source_id == body.canonical_id:
            continue
        source = await session.get(spec.model, source_id)
        if source is None or source.library_id not in library_ids:
            continue
        try:
            await merge(session, source_id, body.canonical_id, actor_id=user.id)
            merged += 1
        except entities.MergeError as error:
            log.warning("skipping %s during unification: %s", source_id, error)

    await session.commit()
    return MergePreviewOut(
        source_id=body.member_ids[0] if body.member_ids else body.canonical_id,
        target_id=body.canonical_id,
        documents=merged,
        detail=f"merged {merged} {spec.label[:-1]} name(s) into {target.name}",
    )
