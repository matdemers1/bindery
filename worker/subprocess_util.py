"""Running external tools with output captured and failures made legible."""

import asyncio
import logging

log = logging.getLogger("bindery.worker.exec")


class CommandError(RuntimeError):
    def __init__(self, argv: list[str], returncode: int, stderr: str) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"{argv[0]} exited {returncode}: {stderr.strip()[:2000]}")


async def run(
    argv: list[str], *, timeout: float, ok_codes: tuple[int, ...] = (0,)
) -> tuple[int, bytes, str]:
    """Run a command, returning (returncode, stdout, stderr).

    Raises CommandError for a code outside `ok_codes`, so a stage that ignores
    the result still fails loudly rather than writing a truncated artifact.
    """
    log.debug("exec %s", " ".join(argv))
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
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
