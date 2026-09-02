"""Where the key lives while unlocked (T-16.3, REQ-179)."""

import uuid
from datetime import UTC, datetime, timedelta

from api.vault import crypto
from api.vault.session import Sessions


def test_a_locked_vault_has_no_key():
    assert Sessions().key(uuid.uuid4()) is None


def test_unlocking_holds_the_key_and_locking_drops_it():
    who, key = uuid.uuid4(), crypto.new_data_key()
    s = Sessions()
    s.unlock(who, key)
    assert s.key(who) == key
    assert s.lock(who) is True
    assert s.key(who) is None


def test_it_expires_on_read_rather_than_on_a_sweep():
    """So there is no moment where an expired key is still reachable — a
    sweeper that runs every minute leaves a minute in which it is."""
    who, key = uuid.uuid4(), crypto.new_data_key()
    s = Sessions(timeout=timedelta(minutes=15))
    s.unlock(who, key)

    later = datetime.now(UTC) + timedelta(minutes=16)
    assert s.key(who, now=later) is None
    assert s.key(who) is None, "the expired entry must be gone, not merely hidden"


def test_using_it_extends_the_window():
    who, key = uuid.uuid4(), crypto.new_data_key()
    s = Sessions(timeout=timedelta(minutes=15))
    s.unlock(who, key)

    assert s.key(who, now=datetime.now(UTC) + timedelta(minutes=10)) == key
    # Ten minutes after that read, so twenty from the unlock — still open,
    # because the clock runs from the last use rather than the unlock.
    assert s.key(who, now=datetime.now(UTC) + timedelta(minutes=20)) == key


def test_a_background_read_does_not_extend_the_window():
    """`peek` is for code that only needs to know, not code acting for a person.

    The vault sweep asks "is this account unlocked?" every 15 seconds. With the
    touching read, that question was itself an answer: any account with a
    vault-bound import had its idle window reset four times a minute and could
    never time out, so ADR-012's 15 minutes quietly became "until the process
    restarts" for exactly the accounts that use the vault most.
    """
    who, key = uuid.uuid4(), crypto.new_data_key()
    s = Sessions(timeout=timedelta(minutes=15))
    s.unlock(who, key)

    ten = datetime.now(UTC) + timedelta(minutes=10)
    assert s.peek(who, now=ten) == key
    # Twenty minutes after the unlock and ten after the peek: the peek did not
    # restart the clock, so this is past the timeout either way.
    assert s.peek(who, now=datetime.now(UTC) + timedelta(minutes=20)) is None


def test_a_background_read_still_relocks_an_idle_vault():
    """Non-touching is not the same as non-expiring: an expired key must be
    gone after a peek, not merely reported as absent."""
    who, key = uuid.uuid4(), crypto.new_data_key()
    s = Sessions(timeout=timedelta(minutes=15))
    s.unlock(who, key)

    assert s.peek(who, now=datetime.now(UTC) + timedelta(minutes=16)) is None
    assert s.key(who) is None, "the expired entry must be dropped, not hidden"


def test_one_persons_unlock_is_not_anothers():
    a, b = uuid.uuid4(), uuid.uuid4()
    s = Sessions()
    s.unlock(a, crypto.new_data_key())
    assert s.key(b) is None


def test_everything_locks_at_once():
    """What a restart does implicitly, and sign-out does explicitly."""
    s = Sessions()
    for _ in range(3):
        s.unlock(uuid.uuid4(), crypto.new_data_key())
    assert s.lock_everything() == 3


def test_the_key_is_never_printable_by_accident():
    """Sessions end up in tracebacks and in anything that logs an object, and
    this application persists every log line to the database.

    The first version of this asserted the *hex* form was absent, which is
    trivially true — `repr(bytes)` is a bytes literal, not hex — so it passed
    while the key was plainly visible in `str(session.__dict__)`. It checks the
    representation that actually appears now.
    """
    who, key = uuid.uuid4(), crypto.new_data_key()
    s = Sessions()
    s.unlock(who, key)

    for rendering in (repr(s), str(s.__dict__), repr(s.__dict__)):
        assert repr(key) not in rendering
        assert repr(key)[2:24] not in rendering, "a prefix of the key is still a leak"
    assert key.hex() not in repr(s)
