"""Stage registry.

Every stage reads a stored artifact and writes a stored artifact, keyed on
`(source_file_id, stage, prompt_version)`. **Stages must be idempotent**
(REQ-111): running one twice produces the same end state, because that is what
makes replay — re-OCR with new settings, re-classify with a new prompt — an
affordable operation rather than a rebuild.

Stages not listed here have not been built yet; a job for one is a programming
error and the runner will dead-letter it rather than pretend it succeeded.
"""

from collections.abc import Awaitable, Callable

from api.db.enums import JobStage
from worker.stages.normalize import run_normalize
from worker.stages.page import run_page

# (session, job) -> None. Raising is how a stage reports failure.
StageFn = Callable[..., Awaitable[None]]

STAGES: dict[JobStage, StageFn] = {
    JobStage.NORMALIZE: run_normalize,
    JobStage.PAGE: run_page,
}

__all__ = ["STAGES", "StageFn"]
