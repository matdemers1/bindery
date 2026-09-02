"""Offsite replication settings (T-13.2, REQ-159, ADR-010).

The credential that ships the archive to S3 is configured in the application,
the same way the Anthropic key is. That means the same two questions apply:
**does the secret ever come back out**, and **does a bad value get accepted and
fail hours later instead of now**.

The third question is specific to this panel. Five fields are saved by one
request, and four of them are useless without the others — so a rejected field
must not leave the other four written. Half-applied configuration is the state
where the screen says one thing and the bucket does another.
"""

import pytest
import sqlalchemy as sa

from api import settings_store
from api.db.models import Setting

# A syntactically valid long-term key: AKIA plus twelve more, per the IAM format.
GOOD_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
GOOD_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


async def _save(client, **fields):
    return await client.put("/api/settings", json=fields)


@pytest.fixture
async def owner(session, signed_in):
    """An administrator, because the offsite panel is now the administrator's.

    Owning a library is not a statement about the host, and every invited
    account owns its own personal library — see `test_a_household_member_cannot_
    repoint_replication` below for what that let anyone do.
    """
    user, _ = await signed_in()
    user.is_admin = True
    await session.commit()
    return user


async def test_the_secret_never_comes_back(client, owner):
    """The rule the whole settings surface is built on.

    A settings form that renders a credential into the DOM has handed it to
    every browser extension the user runs.
    """
    assert (await _save(client, aws_secret_access_key=GOOD_SECRET)).status_code == 200

    body = (await client.get("/api/settings")).json()
    assert body["aws_secret_configured"] is True
    assert body["aws_secret_hint"] == "…EKEY"  # the last four of the secret
    assert GOOD_SECRET not in (await client.get("/api/settings")).text


async def test_the_access_key_id_does_come_back_in_full(client, owner):
    """Deliberate, and the opposite of the rule above.

    An access key id is an identifier, not a credential — CloudTrail logs it and
    the AWS console shows it. Masking it would hide nothing while destroying the
    only question the field answers during a rotation: *which* key is loaded.
    """
    assert (await _save(client, aws_access_key_id=GOOD_KEY_ID)).status_code == 200
    assert (await client.get("/api/settings")).json()["aws_access_key_id"] == GOOD_KEY_ID


async def test_the_kms_key_id_is_stored_in_clear_text(client, owner, session):
    """Because a restore has to read it *without* the archive.

    If the key id were only recoverable by decrypting a database that is itself
    encrypted under that key, it would be unreachable at precisely the moment it
    is needed.
    """
    await _save(client, offsite_kms_key_id="alias/bindery-offsite")

    row = (
        await session.execute(
            sa.select(Setting).where(Setting.key == settings_store.OFFSITE_KMS_KEY_ID)
        )
    ).scalar_one()
    assert row.is_secret is False
    assert row.value == "alias/bindery-offsite"


async def test_the_secret_is_encrypted_at_rest(client, owner, session):
    """The threat is a dump landing somewhere it should not — a backup drive, a
    support ticket — not someone who already owns the host."""
    await _save(client, aws_secret_access_key=GOOD_SECRET)

    row = (
        await session.execute(
            sa.select(Setting).where(Setting.key == settings_store.AWS_SECRET_ACCESS_KEY)
        )
    ).scalar_one()
    assert row.is_secret is True
    assert row.value != GOOD_SECRET
    assert GOOD_SECRET not in row.value


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"offsite_bucket": "Not_A_Valid_Bucket"}, "valid S3 bucket name"),
        ({"offsite_bucket": "ab"}, "valid S3 bucket name"),
        ({"offsite_region": "us-east"}, "AWS region"),
        ({"aws_access_key_id": "ASIAIOSFODNN7EXAMPLE"}, "temporary session credential"),
        # The mistake that actually happens: the two fields filled in swapped.
        ({"aws_access_key_id": GOOD_SECRET}, "access key id"),
    ],
)
async def test_bad_values_are_refused_now_rather_than_at_3am(
    client, owner, fields, expected
):
    """The precedent is the model picker: a bad model id accepted here becomes a
    queue full of dead letters hours later, in a component that did not cause
    it. An unusable bucket name is the same failure with a longer fuse."""
    response = await _save(client, **fields)
    assert response.status_code == 422
    assert expected in response.text


async def test_a_rejected_field_leaves_nothing_written(client, owner):
    """Validation runs before the first write, not between them.

    Otherwise a request naming a good bucket and a bad region saves the bucket,
    refuses, and leaves a configuration that is neither the old one nor the new
    one — with the screen reporting the half that landed.

    This is protected twice over, which took breaking it to establish. Moving
    the validation after the writes does nothing on its own, because
    `get_session` never commits on an exception and the whole transaction is
    discarded. Committing inside the write loop does nothing on its own either,
    because validation-first means the loop never runs. **Both** defects
    together do make this test fail, which is what makes it a test rather than
    an assertion that happens to hold — the first version of it passed with the
    guarantee deliberately broken.

    Asserted against the state *before* the request rather than against `None`:
    this suite migrates once per session, so a test that assumes an empty
    settings table passes or fails on which tests ran before it.
    """
    before = (await client.get("/api/settings")).json()

    response = await _save(
        client,
        aws_access_key_id="AKIA" + "Z" * 16,
        offsite_bucket="bindery-offsite-untouched",
        offsite_region="nonsense",
    )
    assert response.status_code == 422

    after = (await client.get("/api/settings")).json()
    for field in ("aws_access_key_id", "offsite_bucket", "offsite_region"):
        assert after[field] == before[field], f"{field} was written despite the refusal"


async def test_omitting_the_secret_leaves_the_stored_one_alone(client, owner):
    """Changing the region should not require retyping forty characters.

    `None` means leave alone and `""` means clear — the same contract the
    Anthropic key already uses.
    """
    await _save(client, aws_secret_access_key=GOOD_SECRET)
    await _save(client, offsite_region="eu-west-1")

    body = (await client.get("/api/settings")).json()
    assert body["aws_secret_configured"] is True
    assert body["offsite_region"] == "eu-west-1"


async def test_an_empty_secret_clears_it(client, owner):
    """How you turn replication off without losing the rest of the configuration."""
    await _save(client, aws_secret_access_key=GOOD_SECRET)
    await _save(client, aws_secret_access_key="")

    body = (await client.get("/api/settings")).json()
    assert body["aws_secret_configured"] is False
    assert body["aws_secret_hint"] is None


async def test_the_audit_records_the_change_but_never_the_value(client, owner, session):
    from api.db.models import AuditEvent

    await _save(client, aws_secret_access_key=GOOD_SECRET, aws_access_key_id=GOOD_KEY_ID)

    events = (
        await session.execute(
            sa.select(AuditEvent).where(AuditEvent.action == "update_settings")
        )
    ).scalars().all()
    assert events, "a credential change must be auditable"
    recorded = str([event.after for event in events])
    assert "aws_secret_access_key" in recorded
    assert GOOD_SECRET not in recorded


async def test_settings_store_refuses_keys_outside_the_closed_set(session):
    """A stray request cannot invent configuration."""
    with pytest.raises(ValueError):
        await settings_store.set_(session, "aws_session_token", "x", actor_id=None)


# --------------------------------------------------------------------------
# Who may configure the destination (CR-006, ADR-009, ADR-010)
# --------------------------------------------------------------------------


async def test_a_household_member_cannot_repoint_replication(client, signed_in):
    """The whole reason this gate exists.

    The settings table is account-wide and single-row-per-key, so one account's
    write is everyone's configuration — and the old gate was "owns a library",
    which every invited account satisfies by owning its own. A household member
    could therefore name a bucket they control and let the next daily run hand
    them the pg_dump, every blob and every vault object.
    """
    await signed_in()  # an owner of their own library, and not an administrator

    response = await _save(
        client,
        aws_access_key_id=GOOD_KEY_ID,
        aws_secret_access_key=GOOD_SECRET,
        offsite_bucket="attacker-owned-bucket",
        offsite_region="us-east-1",
        offsite_kms_key_id="alias/attacker",
    )
    assert response.status_code == 403
    assert "administrator" in response.text

    body = (await client.get("/api/settings")).json()
    assert body["offsite_bucket"] != "attacker-owned-bucket"


async def test_the_refusal_lands_before_the_first_field_is_written(client, signed_in):
    """A partially applied refusal is the state this panel is built to avoid.

    A single valid-looking field must not slip through on its own — the bucket
    alone is enough to be worth refusing.
    """
    await signed_in()
    before = (await client.get("/api/settings")).json()

    assert (await _save(client, offsite_bucket="attacker-owned-bucket")).status_code == 403

    after = (await client.get("/api/settings")).json()
    assert after["offsite_bucket"] == before["offsite_bucket"]


async def test_probing_the_bucket_is_the_administrators_too(client, signed_in):
    """It spends money and writes to a bucket only an administrator can name."""
    await signed_in()
    assert (await client.post("/api/settings/test-offsite")).status_code == 403


async def test_the_rest_of_the_screen_is_still_the_owners(client, signed_in):
    """The gate is per key, not per screen.

    Choosing a model and adding an API key are library-owner decisions and stay
    that way; narrowing them would take away something an owner has today for
    no gain against this attack.
    """
    await signed_in()
    assert (await _save(client, model="claude-sonnet-5")).status_code == 200


async def test_the_audit_names_the_destination_it_replaced(client, owner, session):
    """"Which bucket was it pointing at before?" is the question a swapped
    destination raises, and `after` alone cannot answer it."""
    from api.db.models import AuditEvent

    await _save(client, offsite_bucket="bindery-offsite-first")
    await _save(client, offsite_bucket="bindery-offsite-second")

    event = (
        await session.execute(
            sa.select(AuditEvent)
            .where(AuditEvent.action == "update_settings")
            .order_by(AuditEvent.sequence.desc())
        )
    ).scalars().first()
    assert event.before["offsite_bucket"] == "bindery-offsite-first"
    assert event.after == {"changed": ["offsite_bucket"]}
