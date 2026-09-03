"""The photo wall (T-8.16) — a top-level screen, in a module named after it.

This lived in `api/routers/logs.py`, tagged `logs`, because it arrived in the
same phase as the log viewer. The cost was not cosmetic: the client half is a
first-class feature folder, so anyone tracing the screen found `features/photos/`
and then had to read twenty routers to find the server half — and `/api/photos`
is the route CLAUDE.md names as having shipped without the vault clause for
exactly that kind of reason. It answers a different question from "where is my
file and what went wrong with it", so it is a different file.

Path, response shape and auth are unchanged; this is a move.
"""

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.dependencies import current_user
from api.db import repository
from api.db.models import AppUser, Classification, Document, MediaMetadata, Page, SourceFile
from api.db.session import get_session
from api.schemas import MediaMetadataOut, PhotoOut, PhotoWallOut
from api.vault.store import VIDEO_SUFFIXES

router = APIRouter(tags=["photos"])

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

# The wall's mandatory predicate used to be ten `original_filename ILIKE '%.jpg'`
# patterns ORed together. A leading wildcard is unindexable by construction, so
# every load was a sequential scan of `document ⨝ source_file` — twice, for the
# count and the rows — driven by pipeline activity on the same Postgres the
# import is writing to.
#
# The same question asked of an *expression* is an equality test, and equality
# tests can be indexed. `substring(name, '\.[^.]*$')` is the last dot and
# everything after it — exactly what `ILIKE '%<ext>'` was testing — so the
# result set is unchanged: a name with no dot yields NULL and matches nothing,
# `a.jpg.pdf` yields `.pdf` and is still not an image, and case is folded the
# way ILIKE folded it. `ix_source_file_extension` (migration 0030) indexes this
# expression, so the pattern is written as a SQL literal rather than a bound
# parameter — a placeholder where the index holds a constant does not match.
_LAST_SUFFIX = sa.literal_column(r"'\.[^.]*$'")


def _extension(column):
    """A filename's lowercased final suffix, including the dot, or NULL."""
    return sa.func.lower(sa.func.substring(column, _LAST_SUFFIX))


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
    kind: str = Query("image", pattern="^(image|video)$", description="photos or videos"),
) -> PhotoWallOut:
    """Every image — or every video — in the archive, as pictures rather than rows.

    A list of filenames is the wrong shape for photographs: you recognise a
    picture instantly and read a filename slowly, so a grid finds things a
    table cannot. It also makes the gap visible — an image OCR could not read
    and nothing has described is, in a list, indistinguishable from any other
    row.
    """
    bound = await repository.scope_for(session, user.id)
    if not bound.visible:
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

    # Whether anything has *looked* at this document — a classification that
    # carried page images. Distinct from whether OCR read anything, which is
    # what the first version of this asked and which no vision pass can change:
    # OCR still finds nothing on a photograph after the photograph has been
    # described, so the wall went on offering to describe it again, at cost.
    looked_at = (
        sa.select(sa.func.coalesce(sa.func.max(Classification.page_images_sent), 0))
        .where(Classification.document_id == Document.id)
        .correlate(Document)
        .scalar_subquery()
    )

    conditions: list[sa.ColumnElement[bool]] = [
        # This screen had no vault boundary at all, so a vaulted photograph
        # stayed on the wall — locked or not. Photographs are the likeliest
        # thing anyone vaults, which made this the one surface that most
        # needed it. `only(Document)` is the library filter and the vault
        # filter as one condition: `api/db/scope.py` assembles it from the
        # visible-library set and `boundary.document_clause`, so the next
        # author cannot take one half and leave the other behind.
        bound.only(Document),
        Document.superseded_at.is_(None),
        _extension(SourceFile.original_filename).in_(
            IMAGE_SUFFIXES if kind == "image" else VIDEO_SUFFIXES
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
            sa.and_(
                sa.or_(text_chars < MIN_USABLE_TEXT, text_chars.is_(None)),
                looked_at == 0,
            )
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
                looked_at.label("looked_at"),
            )
            .join(SourceFile, SourceFile.id == Document.source_file_id)
            .where(sa.and_(*conditions))
            .order_by(SourceFile.received_at.desc(), Document.page_start)
            .limit(limit)
            .offset(offset)
        )
    ).all()

    # One query for every row's metadata rather than one per row. Absent for
    # anything that arrived before Phase 18 read files for it.
    media = {
        row.source_file_id: row
        for row in (
            await session.execute(
                sa.select(MediaMetadata).where(
                    MediaMetadata.source_file_id.in_([r[0].source_file_id for r in rows])
                )
            )
        ).scalars().all()
    } if rows else {}

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
                    and ((row.text_chars or 0) >= MIN_USABLE_TEXT or row.looked_at)
                ),
                kind=kind,
                media=(
                    MediaMetadataOut.model_validate(media[row[0].source_file_id])
                    if row[0].source_file_id in media
                    else None
                ),
            )
            for row in rows
        ],
    )
