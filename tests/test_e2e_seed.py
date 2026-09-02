"""The e2e fixtures, exercised where a browser is not needed (CR-127).

`web/e2e/vault.spec.ts` had two tests that could only ever skip: nothing in CI
put a document into the vault, so `/api/vault/items` was always empty, and both
`test.skip(items.length === 0, …)` and the `test.skip(!state.exists, …)` above
it fired on every run. The documents/photos split and the decrypted-image render
were never executed, and the move-in path — the one code path in Bindery
permitted to delete a person's original (ADR-012) — had no browser coverage at
all.

`scripts/seed-e2e-states.py` now seals one document and one photograph into the
demo account's vault, which is what lets those skips be deleted. That seeding is
the part that can go wrong silently: a browser suite whose fixture quietly did
nothing is exactly the failure being fixed, so the fixture is asserted here,
against a real database, rather than being trusted to have worked.

The script is loaded by path under a name that is not `__main__`, so importing
it does not run it — the CI invocation (`python - < scripts/seed-e2e-states.py`)
still does.

What is *not* asserted here, and is worth knowing: the compose `test` service
bind-mounts `web/src` and `web/public` and not `web/e2e`, so a check that
`vault.spec.ts` no longer skips itself would read the copy baked into the image
and report on a repository that no longer exists — the exact stale-green this
project has already paid for once. Adding `../web/e2e:/app/web/e2e:ro` to the
`test` service would let that guard exist.
"""

import importlib.util
import sys
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

from api.db.models import Document, SourceFile, Vault, VaultItem
from api.storage.blobs import blob_path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "seed-e2e-states.py"


@pytest.fixture(scope="module")
def seed():
    """`scripts/seed-e2e-states.py`, imported rather than executed."""
    assert SCRIPT.is_file(), f"{SCRIPT} is gone; the e2e fixtures have no source"
    spec = importlib.util.spec_from_file_location("bindery_seed_e2e_states", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def test_importing_the_seed_does_not_run_it(seed) -> None:
    """The guard on the guard.

    If the `if __name__ == "__main__":` wrapper is lost, importing this script
    runs `main()` against whatever database the importer is pointed at — which
    would be the test database here and the live archive on a bad day.
    """
    source = SCRIPT.read_text()
    assert 'if __name__ == "__main__":' in source, (
        "seed-e2e-states.py runs on import again. CI pipes it in as `__main__`, "
        "so the wrapper costs nothing and stops an import from seeding a "
        "database nobody asked it to."
    )
    assert callable(seed._a_vault_with_something_in_it)
    assert seed.VAULT_PIN and seed.VAULT_PASSPHRASE


async def test_the_seed_puts_a_document_and_a_photograph_into_the_vault(
    seed, session, user_factory
) -> None:
    """The fixture the two skipped specs needed, asserted end to end.

    Sealing is the real thing — `store.seal`, the same function the route calls
    — so this also exercises the only sanctioned delete path in the project
    against a real blob on disk: encrypt, read back, verify, and only then
    unlink the plaintext.
    """
    from api.vault import service as vault_service
    from api.vault import store as vault_store

    user, library = await user_factory(
        email=f"seed-{uuid.uuid4().hex[:8]}@example.test", library_name="Seeded"
    )

    await seed._a_vault_with_something_in_it(session, user, library.id)
    await session.commit()

    vault = (
        await session.execute(sa.select(Vault).where(Vault.user_id == user.id))
    ).scalar_one()
    items = (
        await session.execute(sa.select(VaultItem).where(VaultItem.vault_id == vault.id))
    ).scalars().all()
    assert len(items) == len(seed.VAULTED), (
        f"the seed left {len(items)} item(s) in the vault; the specs need one "
        "document and one photograph, and an empty vault is what made them skip"
    )

    # The titles are *gone* from `document.title` — sealing moves them into the
    # encrypted metadata with the rest of what the file said about itself — so
    # they are read back the way the screen reads them, through the data key.
    data_key = vault_service.sessions.key(user.id) or await vault_service.unlock_with_pin(
        session, vault, seed.VAULT_PIN
    )
    titles = {
        (vault_store.open_meta(item, data_key).get("title") or "") for item in items
    }
    assert titles == {title for *_, title in seed.VAULTED}, titles

    # Both tabs need something in them: `is_image` is what splits the screen.
    sources = []
    for item in items:
        document = await session.get(Document, item.document_id)
        sources.append(await session.get(SourceFile, document.source_file_id))
    kinds = {
        vault_store.is_image(
            vault_store.media_type_for(item, vault_store.open_meta(item, data_key)),
            source.original_filename,
        )
        for item, source in zip(items, sources, strict=True)
    }
    assert kinds == {True, False}, (
        "the vault does not hold one image and one non-image, so the "
        "documents/photos split has nothing to split"
    )

    # And every one of them is really sealed: vaulted, plaintext gone, pages gone.
    for item in items:
        document = await session.get(Document, item.document_id)
        assert document.vaulted_by == user.id
        source = await session.get(SourceFile, document.source_file_id)
        assert not blob_path(source.sha256).exists(), (
            "the plaintext original is still on disk beside the ciphertext, so "
            "the document was not moved into the vault, it was copied into it"
        )
        pages = (
            await session.execute(
                sa.select(sa.func.count()).select_from(sa.text("page")).where(
                    sa.text("page.source_file_id = :sid")
                ).params(sid=source.id)
            )
        ).scalar_one()
        assert pages == 0, "the page text survived the seal in plaintext"


async def test_running_the_seed_twice_leaves_the_same_vault(
    seed, session, user_factory
) -> None:
    """A seed that is not idempotent works exactly once, which is the same as
    not working — the lesson this script already learned about dead letters."""
    user, library = await user_factory(
        email=f"seed-{uuid.uuid4().hex[:8]}@example.test", library_name="Seeded twice"
    )

    await seed._a_vault_with_something_in_it(session, user, library.id)
    await session.commit()
    await seed._a_vault_with_something_in_it(session, user, library.id)
    await session.commit()

    vault = (
        await session.execute(sa.select(Vault).where(Vault.user_id == user.id))
    ).scalar_one()
    count = (
        await session.execute(
            sa.select(sa.func.count()).select_from(VaultItem).where(
                VaultItem.vault_id == vault.id
            )
        )
    ).scalar_one()
    assert count == len(seed.VAULTED), f"a second run changed the vault to {count} items"


async def test_the_seeded_pin_opens_the_seeded_vault(seed, session, user_factory) -> None:
    """The spec types this PIN into a form. If it does not open the vault, every
    assertion after the unlock fails for a reason that has nothing to do with
    the application."""
    from api.vault import service as vault_service

    user, library = await user_factory(
        email=f"seed-{uuid.uuid4().hex[:8]}@example.test", library_name="Seeded PIN"
    )
    await seed._a_vault_with_something_in_it(session, user, library.id)
    await session.commit()

    vault = (
        await session.execute(sa.select(Vault).where(Vault.user_id == user.id))
    ).scalar_one()
    data_key = await vault_service.unlock_with_pin(session, vault, seed.VAULT_PIN)
    assert len(data_key) == 32
    vault_service.sessions.lock(user.id)
