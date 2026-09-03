"""Whole-host operations are the administrator's, and run one at a time (CR-121).

Four routes read or write the entire pool rather than a library: a full export,
an integrity check, a mirror rebuild, and a backup. Their only gate used to be
`_visible`, which asks whether the caller has a library at all — and every
invited account owns its own (`api/accounts.py`), so it was satisfied by
everyone and said nothing about the host.

Three consequences, which is why the fix has two halves:

- **Resource.** Any account could start an unbounded `pg_dump` plus a copy of
  every blob, repeatedly, on a machine whose single disk pool *is* the archive.
- **Disclosure.** The artefacts land in plaintext under the shared `/data`
  root, where the backlog importer's absolute-path ingest can read them back
  into another account's library.
- **Scope.** `integrity.check` and `run_backup` are unscoped by design. That is
  correct for a host operation and wrong for a route gated on membership.

`admin_only` stays the single definition of who the administrator is (ADR-009),
and `_one_at_a_time` answers 409 rather than starting a second `pg_dump`.
"""

import pytest

from api.routers import trust

# Everything reachable at these paths is a statement about the host.
HOST_OPERATIONS = (
    ("/api/export/full", {"name": "panel"}),
    ("/api/integrity/check", None),
    ("/api/mirror/rebuild", None),
    ("/api/backup/run", None),
)


@pytest.fixture(autouse=True)
def no_slot_left_behind():
    """A test that seeds the in-flight set must not leak it into the next one."""
    yield
    trust._IN_FLIGHT.clear()


@pytest.mark.parametrize("path,body", HOST_OPERATIONS, ids=lambda v: str(v))
async def test_an_ordinary_household_member_is_refused(client, signed_in, path, body):
    """403, and the sentence says why — this is not a probe for something that
    might not exist, it is a capability the caller does not have."""
    await signed_in()

    response = await client.post(path, json=body) if body else await client.post(path)

    assert response.status_code == 403, (path, response.text)
    assert "administrator" in response.json()["detail"], response.text


@pytest.mark.parametrize("path,body", HOST_OPERATIONS, ids=lambda v: str(v))
async def test_the_administrator_is_not_refused(client, session, signed_in, path, body):
    """The other half: the gate must be `admin_only` and not simply closed."""
    user, _library = await signed_in()
    user.is_admin = True
    await session.commit()

    response = await client.post(path, json=body) if body else await client.post(path)

    assert response.status_code != 403, (path, response.text)


@pytest.mark.parametrize("path,body", HOST_OPERATIONS, ids=lambda v: str(v))
async def test_a_second_one_while_one_is_running_is_a_conflict(
    client, session, signed_in, path, body
):
    """One slot for all four, not one each: they read and write the same single
    disk pool, so two different ones at once is the same exhaustion as two of
    the same one. 409 — the request is valid, the state is not."""
    user, _library = await signed_in()
    user.is_admin = True
    await session.commit()
    trust._IN_FLIGHT.add("A backup")

    response = await client.post(path, json=body) if body else await client.post(path)

    assert response.status_code == 409, (path, response.text)
    assert "already running" in response.json()["detail"]


async def test_the_slot_is_released_when_the_operation_finishes(
    client, session, signed_in
):
    """Held by a `finally`, so a failed run does not lock the host out of its
    own maintenance until someone restarts uvicorn."""
    user, _library = await signed_in()
    user.is_admin = True
    await session.commit()

    first = await client.post("/api/integrity/check")
    assert first.status_code == 200, first.text
    assert not trust._IN_FLIGHT

    second = await client.post("/api/integrity/check")
    assert second.status_code == 200, second.text


async def test_the_go_bag_stays_open_to_everyone(client, signed_in):
    """The one export that leaves the building sealed is not a host operation,
    and gating it would be a different product (REQ-092)."""
    await signed_in()
    response = await client.post(
        "/api/export/go-bag", json={"passphrase": "a-long-enough-passphrase"}
    )
    assert response.status_code != 403, response.text
