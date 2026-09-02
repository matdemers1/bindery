"""One bounded thread pool for every Argon2 derivation in this process.

Argon2 is a blocking C call that deliberately costs a great deal of memory:
64 MiB for a password, 256 MiB for a vault secret. Two things follow from that,
and both are why this module exists rather than a bare `asyncio.to_thread`.

**It must not run on the event loop.** A derivation called from inside an
`async def` handler stops every other request — a search, an upload, the health
poll — for as long as it takes. Since ADR-008 the login form faces the open
internet, so "as long as it takes" became a number somebody else chooses.

**Its concurrency must be bounded, not merely moved.** `asyncio.to_thread` uses
the default executor, which is `min(32, cpu + 4)` threads. Thirty-two
concurrent vault derivations is eight gigabytes on a sixteen-gigabyte host,
which is the OOM killer arriving and taking Postgres with it. A small fixed
pool means a burst *queues* rather than allocating: the caller gets slow
answers and the archive stays up.

Queued work costs nothing until it runs, so the queue itself is left unbounded;
`api/auth/throttle.py` is what stops anyone building one worth worrying about.
"""

import asyncio
import functools
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

# Two, not one: one derivation in flight would make a second person's login
# wait behind the first, and two is still only half a gigabyte at the vault's
# parameters. Never more than the machine has cores — on a one-core host,
# parallelism above one buys nothing and costs the memory anyway.
MAX_CONCURRENT = max(1, min(2, os.cpu_count() or 1))

_pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT, thread_name_prefix="bindery-kdf")


async def derive[**P, R](fn: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
    """Run one blocking key derivation off the event loop, in the bounded pool.

    Validate arguments *before* calling this. A refusal that costs a queue slot
    is a refusal an attacker can spend.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_pool, functools.partial(fn, *args, **kwargs))
