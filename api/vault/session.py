"""Where the data key lives while the vault is unlocked (T-16.3, REQ-179).

In this process's memory and nowhere else. Never written to disk, never logged,
never sent to the client, and dropped on a timeout, on sign-out, and whenever
the process restarts — which is why a deploy relocks every vault, and that is
the correct behaviour rather than an inconvenience.

The cost of holding it here at all is stated plainly in ADR-012: while unlocked,
this server can read the vault, and so could anyone who compromised it in that
window. What it buys is that a vaulted document is an ordinary document — OCR'd,
paged, searchable. A vault whose contents cannot be found is a folder.
"""

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

log = logging.getLogger("bindery.vault")

# Short by design. The vault is opened to do a thing and then should close
# behind you; a session that outlives the reason for it is the laptop-on-the-
# kitchen-table problem the feature exists to solve.
IDLE_TIMEOUT = timedelta(minutes=15)


@dataclass
class _Unlocked:
    # `repr=False`, and it matters: this object ends up inside tracebacks and
    # anything that prints a session. The default dataclass repr rendered the
    # key as a bytes literal, which a single unhandled exception would have put
    # in the log the whole application writes to the database.
    data_key: bytes = field(repr=False)
    touched_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class Sessions:
    """Unlocked vaults, by user.

    A plain dict behind a lock rather than anything shared: this state is
    deliberately per-process and non-durable. Two api containers would each
    need their own unlock, which is a property worth keeping — an unlock is a
    thing a person did *here*, not a fact stored somewhere.
    """

    def __init__(self, timeout: timedelta = IDLE_TIMEOUT) -> None:
        self._by_user: dict[uuid.UUID, _Unlocked] = {}
        self._guard = threading.Lock()
        self._timeout = timeout

    def unlock(self, user_id: uuid.UUID, data_key: bytes) -> None:
        with self._guard:
            self._by_user[user_id] = _Unlocked(data_key, datetime.now(UTC))
        log.info("vault unlocked for %s", user_id)

    def key(self, user_id: uuid.UUID, *, now: datetime | None = None) -> bytes | None:
        """The data key, or None. Touching it extends the idle window.

        For calls made on a person's behalf — an unlock, a search, opening a
        document, moving one in or out. Code that only needs to *know* whether
        a vault is open wants `peek`.
        """
        return self._read(user_id, now=now, extend=True)

    def peek(self, user_id: uuid.UUID, *, now: datetime | None = None) -> bytes | None:
        """The data key, or None, without counting as use.

        The vault sweep asks whether each vault-bound import's owner is
        unlocked, four times a minute. Asking through `key` answered itself:
        any account with one such import — the ordinary state after any vault
        import, since nothing clears the flag — had its idle window reset on
        every tick and could never time out, so ADR-012's fifteen minutes
        silently became "until the process restarts" for exactly the accounts
        that use the vault most. Nobody could see it: the screen said unlocked,
        and it was.

        Non-touching, not non-expiring — an expired key is still dropped here
        rather than merely reported as absent.
        """
        return self._read(user_id, now=now, extend=False)

    def _read(
        self, user_id: uuid.UUID, *, now: datetime | None, extend: bool
    ) -> bytes | None:
        now = now or datetime.now(UTC)
        with self._guard:
            held = self._by_user.get(user_id)
            if held is None:
                return None
            if now - held.touched_at > self._timeout:
                # Dropped on read rather than by a sweeper: there is no moment
                # where an expired key is still reachable, even briefly.
                del self._by_user[user_id]
                log.info("vault relocked for %s after idling", user_id)
                return None
            if extend:
                held.touched_at = now
            return held.data_key

    def lock(self, user_id: uuid.UUID) -> bool:
        with self._guard:
            return self._by_user.pop(user_id, None) is not None

    def lock_everything(self) -> int:
        with self._guard:
            count = len(self._by_user)
            self._by_user.clear()
        return count

    def is_unlocked(self, user_id: uuid.UUID) -> bool:
        """Asked on a person's behalf, so it counts as use, like `key`.

        A background reader must use `peek`, or its own polling keeps the vault
        open. So must a request handler a screen *polls* — this docstring used
        to claim every caller was a person's own action, and the Import status
        route disproved it on a five-second timer. The test is whether the read
        happens because somebody did something, not whether it is inside a
        request.
        """
        return self.key(user_id) is not None

    def __repr__(self) -> str:
        # Never the contents. The count is the useful part and the keys are the
        # part that must not be printable by accident.
        return f"<Sessions unlocked={len(self._by_user)}>"


# One per process, which is the whole design.
sessions = Sessions()
