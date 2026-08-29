"""The log viewer, and per-file pipeline progress (T-8.11, T-8.12).

Two questions this answers that nothing previously could without a shell on the
host: *where is my file right now*, and *what went wrong with it*.
"""

import uuid
from datetime import datetime

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from api import eventlog
from api.auth.dependencies import current_user
from api.db import repository
from api.db.enums import JobState, SourceFileState
from api.db.models import AppUser, Document, EventLog, Job, Page, SourceFile
from api.db.session import get_session
from api.schemas import (
    FileProgressOut,
    LogEntryOut,
    LogPageOut,
    PhotoOut,
    PhotoWallOut,
    PipelineFilesOut,
)

router = APIRouter(tags=["logs"])

# The order a file moves through, which is what the visual pipeline draws.
# `duplicate` and `failed` are ends rather than steps, so they are not here.
STAGE_ORDER = [
    SourceFileState.RECEIVED,
    SourceFileState.NORMALIZING,
    SourceFileState.PAGING,
    SourceFileState.SEGMENTING,
    SourceFileState.PROCESSED,
]


@router.get("/logs", response_model=LogPageOut)
async def read_logs(
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
    source_file_id: uuid.UUID | None = None,
    level: str | None = Query(None, description="minimum level: info, warning, error"),
    q: str | None = Query(None, description="substring of the message"),
    since: datetime | None = None,
    before_sequence: int | None = Query(None, description="keyset cursor"),
    limit: int = Query(100, ge=1, le=500),
) -> LogPageOut:
    """Diagnostics, newest first.

    Scoped like everything else: a log line tagged to a library you are not in
    is not yours to read, because log messages routinely contain filenames.
    Lines with no library are system-level — the worker starting, a health pass —
    and carry no document content.
    """
    library_ids = await repository.visible_library_ids(session, user.id)
    if not library_ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no visible libraries")

    conditions: list[sa.ColumnElement[bool]] = [
        sa.or_(EventLog.library_id.in_(library_ids), EventLog.library_id.is_(None))
    ]
    if source_file_id is not None:
        conditions.append(EventLog.source_file_id == source_file_id)
    if level:
        # A minimum, not an exact match: asking for warnings and being shown no
        # errors would be the opposite of useful.
        wanted = {"error": ["error", "critical"], "warning": ["warning", "error", "critical"]}
        conditions.append(EventLog.level.in_(wanted.get(level, [level])))
    if q:
        conditions.append(EventLog.message.ilike(f"%{q}%"))
    if since is not None:
        conditions.append(EventLog.created_at >= since)
    if before_sequence is not None:
        conditions.append(EventLog.sequence < before_sequence)

    rows = (
        (
            await session.execute(
                sa.select(EventLog)
                .where(sa.and_(*conditions))
                .order_by(EventLog.sequence.desc())
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )

    has_more = len(rows) > limit
    entries = [LogEntryOut.model_validate(row) for row in rows[:limit]]
    return LogPageOut(
        entries=entries,
        next_before_sequence=entries[-1].sequence if has_more and entries else None,
        # Surfaced rather than hidden: if the queue is backing up, the logs you
        # are reading are incomplete and you should know that while reading them.
        pending_writes=eventlog.pending(),
    )


@router.get("/pipeline/files", response_model=PipelineFilesOut)
async def pipeline_files(
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
    ids: list[uuid.UUID] | None = Query(None, description="specific files to follow"),
    limit: int = Query(25, ge=1, le=200),
) -> PipelineFilesOut:
    """Where each file is in the pipeline, for the visual view.

    A file's own `state` column is the source of truth for position — it is what
    the stages themselves set — and the job rows supply what is *happening*
    right now and what went wrong.
    """
    library_ids = await repository.visible_library_ids(session, user.id)
    if not library_ids:
        return PipelineFilesOut(files=[], stages=[state.value for state in STAGE_ORDER])

    query = sa.select(SourceFile).where(SourceFile.library_id.in_(library_ids))
    if ids:
        query = query.where(SourceFile.id.in_(ids))
    files = (
        (await session.execute(query.order_by(SourceFile.received_at.desc()).limit(limit)))
        .scalars()
        .all()
    )
    if not files:
        return PipelineFilesOut(files=[], stages=[state.value for state in STAGE_ORDER])

    file_ids = [file.id for file in files]
    jobs = (
        (
            await session.execute(
                sa.select(Job)
                .where(Job.source_file_id.in_(file_ids))
                .order_by(Job.updated_at.desc())
            )
        )
        .scalars()
        .all()
    )
    by_file: dict[uuid.UUID, list[Job]] = {}
    for job in jobs:
        by_file.setdefault(job.source_file_id, []).append(job)

    document_counts = dict(
        (
            await session.execute(
                sa.select(Document.source_file_id, sa.func.count())
                .where(Document.source_file_id.in_(file_ids), Document.superseded_at.is_(None))
                .group_by(Document.source_file_id)
            )
        ).all()
    )

    out = []
    for file in files:
        related = by_file.get(file.id, [])
        running = next((j for j in related if j.state == JobState.RUNNING), None)
        broken = next(
            (j for j in related if j.state in (JobState.DEAD_LETTER, JobState.FAILED)), None
        )
        out.append(
            FileProgressOut(
                source_file_id=file.id,
                original_filename=file.original_filename,
                byte_size=file.byte_size,
                page_count=file.page_count,
                state=str(file.state),
                received_at=file.received_at,
                ingest_source=str(file.ingest_source),
                document_count=document_counts.get(file.id, 0),
                active_stage=str(running.stage) if running else None,
                failed_stage=str(broken.stage) if broken else None,
                # Verbatim. A paraphrased error is a second bug to debug.
                last_error=broken.last_error if broken else None,
                attempts=broken.attempts if broken else 0,
                dead_lettered=bool(broken and broken.state == JobState.DEAD_LETTER),
            )
        )

    return PipelineFilesOut(files=out, stages=[state.value for state in STAGE_ORDER])


# --------------------------------------------------------------------------
# The photo wall (T-8.16)
# --------------------------------------------------------------------------

# What a person means by "my images": things that were photographed or drawn,
# not a 40-page PDF that happens to contain a logo.
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff",
                  ".heic", ".heif")

# Below this many characters there was effectively nothing to read, and a
# classification derived from the text is a classification derived from
# nothing. It is the same threshold `worker.stages.classify` uses to decide a
# document needs to be *looked* at, deliberately: the filter that finds these
# pictures and the pass that fixes them must agree on what "unreadable" means.
MIN_USABLE_TEXT = 40


@router.get("/photos", response_model=PhotoWallOut)
async def photos(
    session: AsyncSession = Depends(get_session),
    user: AppUser = Depends(current_user),
    q: str | None = Query(None, description="substring of the title or filename"),
    undescribed: bool = Query(
        False, description="only ones nothing could be read from, and nothing has looked at"
    ),
    limit: int = Query(120, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> PhotoWallOut:
    """Every image in the archive, as pictures rather than as rows.

    A list of filenames is the wrong shape for photographs: you recognise a
    picture instantly and read a filename slowly, so a grid finds things a
    table cannot. It also makes the gap visible — an image OCR could not read
    and nothing has described is, in a list, indistinguishable from any other
    row.
    """
    library_ids = await repository.visible_library_ids(session, user.id)
    if not library_ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no visible libraries")

    # The text of the page the document starts on. A photograph is one page, so
    # for this view "the first page" and "the page" are the same thing.
    text_chars = (
        sa.select(sa.func.coalesce(sa.func.length(Page.text), 0))
        .where(
            Page.source_file_id == Document.source_file_id,
            Page.page_number == Document.page_start,
        )
        .correlate(Document)
        .scalar_subquery()
    )

    conditions: list[sa.ColumnElement[bool]] = [
        Document.library_id.in_(library_ids),
        Document.superseded_at.is_(None),
        sa.or_(
            *[SourceFile.original_filename.ilike(f"%{ext}") for ext in IMAGE_SUFFIXES]
        ),
    ]
    if q:
        conditions.append(
            sa.or_(
                Document.title.ilike(f"%{q}%"),
                SourceFile.original_filename.ilike(f"%{q}%"),
                Document.summary.ilike(f"%{q}%"),
            )
        )
    if undescribed:
        # The first version of this asked whether the title was NULL, and found
        # nothing: classification had already run on all 182 images and written
        # a title for every one of them. The titles were "Unreadable Scan",
        # "Blank or Unreadable Scan", "Unidentified Correspondent - ERS" —
        # summaries of an empty string, confidently phrased. A row being
        # populated is not evidence that anything knows what the picture is.
        #
        # So ask the honest question instead: was there anything to read? These
        # are the pictures a description pass would actually help, and they are
        # exactly the ones the classify stage now sends as images.
        conditions.append(
            sa.or_(text_chars < MIN_USABLE_TEXT, text_chars.is_(None))
        )

    total = await session.scalar(
        sa.select(sa.func.count())
        .select_from(Document)
        .join(SourceFile, SourceFile.id == Document.source_file_id)
        .where(sa.and_(*conditions))
    )

    rows = (
        await session.execute(
            sa.select(
                Document,
                SourceFile.original_filename,
                SourceFile.received_at,
                text_chars.label("text_chars"),
            )
            .join(SourceFile, SourceFile.id == Document.source_file_id)
            .where(sa.and_(*conditions))
            .order_by(SourceFile.received_at.desc(), Document.page_start)
            .limit(limit)
            .offset(offset)
        )
    ).all()

    return PhotoWallOut(
        total=total or 0,
        photos=[
            PhotoOut(
                document_id=row[0].id,
                source_file_id=row[0].source_file_id,
                page=row[0].page_start,
                title=row[0].title,
                summary=row[0].summary,
                original_filename=row.original_filename,
                received_at=row.received_at,
                document_date=row[0].document_date,
                text_chars=int(row.text_chars or 0),
                described=bool(
                    row[0].title
                    and row[0].summary
                    and (row.text_chars or 0) >= MIN_USABLE_TEXT
                ),
            )
            for row in rows
        ],
    )
