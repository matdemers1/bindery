"""The api refuses to serve an unconfigured install (CR-014, ADR-008).

Since 2026-08-30 Bindery's own login page faces the open internet with nothing
in front of it. A `JWT_SECRET` that is still the placeholder from
`.env.example` produces an app that looks completely normal and signs every
session with a value published in this repository — and `api/settings_store.py`
derives the key protecting the stored AWS and Anthropic credentials from that
same string.

There is no wrong-looking screen to notice, which is why this is a startup
refusal rather than a warning: a wrong `DATABASE_URL` fails loudly on the first
query, and a wrong `JWT_SECRET` never fails at all.
"""

import pytest

from api import config, main
from api.config import (
    PLACEHOLDER_JWT_SECRET,
    Settings,
    UnusableConfiguration,
    configuration_problems,
    require_usable_configuration,
)

GOOD_SECRET = "3rEZ9nrIiVQmSuXcRIrdlqOsm3ImTHNqEvVsFuiRP18"
GOOD_URL = "postgresql+asyncpg://bindery:a-real-password@postgres:5432/bindery"


def usable(**overrides) -> Settings:
    return Settings(**{"jwt_secret": GOOD_SECRET, "database_url": GOOD_URL, **overrides})


def test_the_shipped_defaults_are_refused() -> None:
    """Constructed with nothing set at all — a host rebuild that lost its .env."""
    problems = configuration_problems(
        Settings(jwt_secret=PLACEHOLDER_JWT_SECRET, database_url="postgresql://x/change-me")
    )
    assert len(problems) == 2
    assert any("JWT_SECRET" in problem for problem in problems)
    assert any("DATABASE_URL" in problem for problem in problems)


def test_the_message_names_the_variable_and_what_to_set_it_to() -> None:
    """The person reading this is looking at a container that will not start,
    at an hour they did not choose. The message is the whole diagnosis."""
    problem = configuration_problems(usable(jwt_secret=PLACEHOLDER_JWT_SECRET))[0]
    assert "JWT_SECRET" in problem
    assert "openssl rand" in problem


@pytest.mark.parametrize(
    "secret",
    [
        PLACEHOLDER_JWT_SECRET,
        "change-me",
        "a-change-me-with-more-around-it",
        "",
        "   ",
        "short",
    ],
)
def test_placeholders_and_stubs_are_all_refused(secret: str) -> None:
    assert configuration_problems(usable(jwt_secret=secret))


def test_a_real_configuration_passes() -> None:
    assert configuration_problems(usable()) == []
    require_usable_configuration(usable())


def test_the_values_ci_and_the_deployed_host_actually_pass() -> None:
    """The check has to be true of the configurations that already exist, or
    the first thing it does is stop the build.

    `.github/workflows/build.yml` writes a deliberately short secret, and the
    floor is set below it on purpose — this is a placeholder trap, not an
    entropy meter.
    """
    assert configuration_problems(usable(jwt_secret="ci-only-not-a-real-secret")) == []
    assert (
        configuration_problems(
            usable(database_url="postgresql+asyncpg://bindery:ci@postgres:5432/bindery")
        )
        == []
    )


async def test_the_app_refuses_to_start_with_the_default_secret(monkeypatch) -> None:
    """The refusal has to be wired into `lifespan`, not merely available.

    A validator nobody calls is the same as no validator, and this one has to
    fire before the first request rather than at the first login.
    """
    monkeypatch.setattr(
        main, "get_settings", lambda: usable(jwt_secret=PLACEHOLDER_JWT_SECRET)
    )

    with pytest.raises(UnusableConfiguration) as raised:
        async with main.lifespan(main.app):
            pytest.fail("the api started with a publicly known signing key")

    assert "JWT_SECRET" in str(raised.value)


def test_the_settings_store_key_is_derived_from_the_secret_being_checked() -> None:
    """Why the refusal covers more than sessions.

    If this derivation moves to its own variable, this check has to grow a
    second one — otherwise the credentials that ship the archive offsite go
    back to being protected by a public string.
    """
    import inspect

    from api import settings_store

    assert "jwt_secret" in inspect.getsource(settings_store)
    assert config.PLACEHOLDER_MARKER == "change-me"
