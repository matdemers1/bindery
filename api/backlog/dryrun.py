"""The dry run (REQ-082). Nothing is processed until this has been seen.

> Point the importer at the real archive directory. **Review the dry run.**

Three questions it has to answer before a human commits: how much is there, how
much of it do I already have, and what will this cost. The third is what bounds
**R-06** — segmentation cost ballooning across a bundle-heavy backlog — with a
number rather than a hope.

The cost model is deliberately crude and deliberately pessimistic. A projection
that is 30% high causes a moment of hesitation; one that is 50% low causes a
bill nobody agreed to.
"""

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.backlog.walker import WalkResult, hash_file
from api.db.models import SourceFile

log = logging.getLogger("bindery.import.dryrun")

# Rough page counts, used only for projection. A PDF is measured properly; an
# image is one page by definition.
ASSUMED_PDF_PAGES = 8
# OCR text runs about 500 tokens per page; the prompt adds instructions and the
# candidate taxonomy on top.
TOKENS_PER_PAGE = 500
PROMPT_OVERHEAD_TOKENS = 1200
OUTPUT_TOKENS = 400
# claude-opus-5, USD per million. Batch halves both.
INPUT_COST_PER_MTOK = 5.00
OUTPUT_COST_PER_MTOK = 25.00
BATCH_DISCOUNT = 0.5
# R-06's tripwire.
COST_ALARM_USD = 150.0


@dataclass
class DryRun:
    root: str
    total_files: int = 0
    total_bytes: int = 0
    by_extension: dict[str, int] = field(default_factory=dict)
    already_in_archive: int = 0
    duplicates_within_batch: int = 0
    new_files: int = 0
    estimated_pages: int = 0
    skipped_unsupported: int = 0
    skipped_hidden: int = 0
    skipped_too_large: int = 0
    errors: list[dict] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class CostEstimate:
    estimated_pages: int
    input_tokens: int
    output_tokens: int
    interactive_usd: float
    batch_usd: float
    # True when the projection crosses R-06's tripwire and the plan says to fall
    # back to heuristics-only segmentation plus manual correction.
    exceeds_alarm: bool
    alarm_threshold_usd: float = COST_ALARM_USD

    def to_json(self) -> dict:
        return asdict(self)


def _pdf_pages(path: Path) -> int:
    try:
        import pikepdf

        with pikepdf.open(path) as pdf:
            return len(pdf.pages)
    except Exception:
        # A PDF we cannot open is still going to be attempted; assume the
        # average rather than dropping it from the projection.
        return ASSUMED_PDF_PAGES


def estimate_cost(pages: int) -> CostEstimate:
    input_tokens = pages * TOKENS_PER_PAGE + PROMPT_OVERHEAD_TOKENS * max(pages // 8, 1)
    output_tokens = OUTPUT_TOKENS * max(pages // 8, 1)
    interactive = (
        input_tokens / 1_000_000 * INPUT_COST_PER_MTOK
        + output_tokens / 1_000_000 * OUTPUT_COST_PER_MTOK
    )
    return CostEstimate(
        estimated_pages=pages,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        interactive_usd=round(interactive, 2),
        batch_usd=round(interactive * BATCH_DISCOUNT, 2),
        exceeds_alarm=round(interactive * BATCH_DISCOUNT, 2) > COST_ALARM_USD,
    )


async def analyse(
    session: AsyncSession, root: Path, result: WalkResult, *, hash_limit: int = 5000
) -> tuple[DryRun, CostEstimate]:
    """Count, deduplicate, and project. Reads nothing into the archive."""
    report = DryRun(
        root=str(root),
        total_files=len(result.files),
        total_bytes=result.total_bytes,
        skipped_unsupported=result.skipped_unsupported,
        skipped_hidden=result.skipped_hidden,
        skipped_too_large=result.skipped_too_large,
        errors=[{"path": path, "error": message} for path, message in result.errors[:100]],
    )

    for path in result.files:
        extension = path.suffix.lower()
        report.by_extension[extension] = report.by_extension.get(extension, 0) + 1

    # Hashing is the expensive part of a dry run, so it is bounded. Above the
    # limit the duplicate count becomes an estimate, which is said plainly
    # rather than presented as fact.
    seen: set[str] = set()
    pages = 0
    for path in result.files[:hash_limit]:
        digest = hash_file(path)
        if digest is None:
            continue
        if digest in seen:
            report.duplicates_within_batch += 1
            continue
        seen.add(digest)

    if seen:
        known = set(
            (
                await session.execute(
                    sa.select(SourceFile.sha256).where(SourceFile.sha256.in_(list(seen)))
                )
            ).scalars().all()
        )
        report.already_in_archive = len(known)

    for path in result.files:
        pages += _pdf_pages(path) if path.suffix.lower() == ".pdf" else 1

    report.estimated_pages = pages
    report.new_files = max(
        report.total_files - report.already_in_archive - report.duplicates_within_batch, 0
    )
    return report, estimate_cost(pages)
