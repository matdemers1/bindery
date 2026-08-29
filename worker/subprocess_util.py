"""Running external tools with output captured and failures made legible."""

import asyncio
import logging

log = logging.getLogger("bindery.worker.exec")


# Ghostscript and friends print a page of progress before the line that matters,
# so a message trimmed from the front keeps the noise and loses the cause.
HEAD_CHARS = 400
TAIL_CHARS = 1600


def summarize(stderr: str) -> str:
    """Keep both ends of a long error, because the cause could be at either.

    A tool that fails at startup says so immediately; a tool that fails on page
    six says so after six pages of font-loading chatter. Keeping only the head
    truncated a Ghostscript failure mid-log and left no way to tell what had
    gone wrong.
    """
    text = stderr.strip()
    if len(text) <= HEAD_CHARS + TAIL_CHARS:
        return text
    dropped = len(text) - HEAD_CHARS - TAIL_CHARS
    return f"{text[:HEAD_CHARS]}\n… [{dropped} characters omitted] …\n{text[-TAIL_CHARS:]}"


class CommandError(RuntimeError):
    def __init__(self, argv: list[str], returncode: int, stderr: str) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"{argv[0]} exited {returncode}: {summarize(stderr)}")


async def run(
    argv: list[str],
    *,
    timeout: float,
    ok_codes: tuple[int, ...] = (0,),
    env: dict[str, str] | None = None,
) -> tuple[int, bytes, str]:
    """Run a command, returning (returncode, stdout, stderr).

    Raises CommandError for a code outside `ok_codes`, so a stage that ignores
    the result still fails loudly rather than writing a truncated artifact.

    `env` replaces the child's environment entirely when given — LibreOffice
    needs a writable HOME of its own, and inheriting the worker's would let two
    conversions collide in one profile.
    """
    log.debug("exec %s", " ".join(argv))
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise CommandError(argv, -1, f"timed out after {timeout}s") from None

    decoded_err = stderr.decode("utf-8", "replace")
    if process.returncode not in ok_codes:
        raise CommandError(argv, process.returncode or -1, decoded_err)
    return process.returncode or 0, stdout, decoded_err
