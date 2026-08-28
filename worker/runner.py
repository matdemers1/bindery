"""Pipeline worker entrypoint.

Placeholder. The real queue consumer — SELECT … FOR UPDATE SKIP LOCKED with
retries, backoff and a dead-letter state — is Phase 1, T-1.1. For now this keeps
the container alive and proves it can reach the database, so `docker compose ps`
reports a real result rather than a crash loop.
"""

import asyncio
import logging

import sqlalchemy as sa

from api.config import get_settings
from api.db.session import SessionFactory, engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("bindery.worker")

IDLE_SECONDS = 30


async def main() -> None:
    settings = get_settings()
    log.info(
        "worker starting; concurrency=%s data_root=%s",
        settings.worker_concurrency,
        settings.data_root,
    )

    while True:
        try:
            async with SessionFactory() as session:
                await session.execute(sa.text("SELECT 1"))
            log.info("no stages registered yet (queue runner lands in Phase 1, T-1.1)")
        except Exception:
            log.exception("database unreachable; retrying")
        await asyncio.sleep(IDLE_SECONDS)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        asyncio.run(engine.dispose())
