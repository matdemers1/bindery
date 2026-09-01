#!/usr/bin/env python3
"""Capture a screenshot of every documented screen, from the running app.

    make screenshots

Documentation screenshots rot silently. Nothing fails; the picture is simply of
an application that no longer looks like that, which is worse than no picture at
all because it is believed. The operator's requirement was that every feature
change updates the docs — which is exactly the thing nobody does — so this makes
it one command, and `tests/test_docs.py` fails the build when the command has
not been run.

The manifest is the part that matters. Each entry records the commit timestamp
the shot was taken at and the source paths that screen is built from; the test
asks git when those paths last changed. That is deterministic, unlike a pixel
comparison, which differs by machine on antialiasing and font hinting alone and
would be switched off within a month of being introduced.

Requires Playwright. Deliberately not a project dependency — nothing in the
running system needs a browser, and the capture is a development task:

    pip install playwright && playwright install chromium
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GUIDES = REPO / "web" / "public" / "help" / "guides.json"
OUT = REPO / "web" / "public" / "help" / "screens"

BASE_URL = os.environ.get("BINDERY_URL", "http://localhost:8080")
EMAIL = os.environ.get("BINDERY_EMAIL", "")
PASSWORD = os.environ.get("BINDERY_PASSWORD", "")

# What each screen is built from. Used only to decide staleness — a screen whose
# source has changed since its picture was taken needs a new picture.
SOURCES: dict[str, list[str]] = {
    "/": ["web/src/features/ask", "web/src/features/firstrun"],
    "/search": ["web/src/features/search"],
    "/archive": ["web/src/features/archive"],
    "/files": ["web/src/features/files"],
    "/photos": ["web/src/features/photos"],
    "/vault": ["web/src/features/vault"],
    "/add": ["web/src/features/add"],
    "/review": ["web/src/features/review"],
    "/organise": ["web/src/features/organise"],
    "/import": ["web/src/features/import"],
    "/rules": ["web/src/features/rules"],
    "/trust": ["web/src/features/trust"],
    "/pipeline": ["web/src/features/pipeline"],
    "/account": ["web/src/features/accounts/AccountPage.tsx"],
    "/people": ["web/src/features/accounts/AdminPage.tsx"],
    "/settings": ["web/src/features/settings"],
}

# Every screen also depends on the shell around it.
SHARED = ["web/src/components/Shell.tsx", "web/src/index.css"]


def _source_commit(paths: list[str]) -> str:
    """The commit that last changed any of these paths.

    A *hash*, not a timestamp. The first version recorded HEAD's timestamp and
    compared it against the source's — which fired the moment the screenshots
    were committed, because that commit is newer than everything it contains.
    Every capture immediately invalidated itself.

    A hash of the sources has no such problem: committing a picture does not
    change the code it is a picture of, so the recorded value still matches.
    And when somebody *does* change a screen, it stops matching, which is the
    entire point.
    """
    out = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", *paths], cwd=REPO,
        capture_output=True, text=True, check=False,
    )
    return out.stdout.strip() or "unknown"


async def _prepare_vault(page) -> None:
    """Create and unlock a vault on the demonstration stack, best effort.

    Deliberately does not move a document in. Vaulting deletes the plaintext
    original, and a screenshot script that quietly destroys one of the seeded
    documents every time it runs would be a bad trade for a nicer picture.
    """
    result = await page.evaluate(
        """async () => {
            const state = await fetch('/api/vault', {credentials: 'same-origin'})
              .then((r) => r.json());
            if (state.exists) {
              if (state.unlocked) return 'already open';
              return 'exists but locked — not guessing at its secret';
            }
            const made = await fetch('/api/vault/setup', {
              method: 'POST',
              credentials: 'same-origin',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({
                passphrase: 'demonstration vault passphrase',
                pin: '481516',
              }),
            });
            return made.ok ? 'created' : `setup failed: ${made.status}`;
        }"""
    )
    print(f"  vault:           {result}")

async def main() -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print(
            "Playwright is not installed. It is not a project dependency — "
            "nothing in the running system needs a browser.\n"
            "  pip install playwright && playwright install chromium",
            file=sys.stderr,
        )
        return 2

    if not EMAIL or not PASSWORD:
        print(
            "Set BINDERY_EMAIL and BINDERY_PASSWORD to an account on "
            f"{BASE_URL}. Use a throwaway account with demonstration documents: "
            "these pictures go in the repository.",
            file=sys.stderr,
        )
        return 2

    guides = json.loads(GUIDES.read_text())["guides"]
    # Re-shooting fifteen screens to document one new one is churn: every
    # unrelated picture changes because the seeded data moved on a little.
    only = {arg for arg in sys.argv[1:] if not arg.startswith("-")}
    if only:
        guides = [
            guide for guide in guides
            if guide["route"] in only or guide["screenshot"] in only
        ]
        if not guides:
            print(f"nothing matches {sorted(only)}", file=sys.stderr)
            return 2
    OUT.mkdir(parents=True, exist_ok=True)
    entries = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900})

        await page.goto(f"{BASE_URL}/", wait_until="networkidle")
        await page.fill('input[autocomplete="username"]', EMAIL)
        await page.fill('input[autocomplete="current-password"]', PASSWORD)
        await page.click('button[type="submit"]')
        await page.wait_for_load_state("networkidle")

        if await page.locator('input[autocomplete="one-time-code"]').count():
            print(
                "This account has two-factor enabled, which cannot be scripted. "
                "Capture with an account that does not.",
                file=sys.stderr,
            )
            await browser.close()
            return 2

        # Prove we are actually inside before taking fifteen pictures.
        #
        # The first run of this did not, and cheerfully wrote fifteen identical
        # screenshots of the login form — then reported success. A tool for
        # keeping documentation honest that silently produces wrong
        # documentation is worse than no tool.
        try:
            await page.wait_for_selector('nav[aria-label="Sections"]', timeout=10_000)
        except Exception:
            problem = await page.locator("form p.text-red-400").first.text_content() \
                if await page.locator("form p.text-red-400").count() else None
            print(
                f"Sign-in did not succeed for {EMAIL}"
                + (f": {problem.strip()}" if problem else "")
                + f"\nNothing was captured. Check the account exists on {BASE_URL} "
                "and that the stack is running the current build.",
                file=sys.stderr,
            )
            await page.screenshot(path="/tmp/signin-failure.png")
            print(
                f"  url now: {page.url}\n"
                f"  page text: {(await page.locator('body').inner_text())[:300]!r}",
                file=sys.stderr,
            )
            await browser.close()
            return 1

        # The vault screen shows a setup form until a vault exists, and a guide
        # about searching and restoring illustrated by an empty create-account
        # form is a guide that documents the wrong screen. So one is made here,
        # in the demonstration stack, with a throwaway secret.
        await _prepare_vault(page)

        for guide in guides:
            route, shot = guide["route"], guide["screenshot"]
            await page.goto(f"{BASE_URL}{route}", wait_until="networkidle")
            # Live updates and lazy images settle a moment after the network
            # goes quiet; without this the shot catches half-drawn screens.
            await page.wait_for_timeout(1200)

            # Session expiry, a crash, a redirect: any of them would otherwise
            # be captured as a perfectly good picture of the wrong thing.
            if not await page.locator('nav[aria-label="Sections"]').count():
                print(
                    f"{route} did not render the application shell — signed out "
                    "or crashed. Nothing further captured.",
                    file=sys.stderr,
                )
                await browser.close()
                return 1

            await page.screenshot(path=str(OUT / shot))
            sources = SOURCES.get(route, []) + SHARED
            entries.append(
                {
                    "route": route,
                    "screenshot": shot,
                    "sources": sources,
                    "source_commit": _source_commit(sources),
                }
            )
            print(f"  {shot:16} {route}")

        await browser.close()

    # Merged, never replaced. A partial run that overwrote the manifest with
    # only the screens it shot would drop the staleness record for every other
    # one — and the check would then pass by knowing nothing about them, which
    # is the exact failure `test_docs.py` exists to prevent.
    manifest = OUT / "manifest.json"
    kept = []
    if manifest.is_file():
        shot_now = {entry["screenshot"] for entry in entries}
        kept = [
            entry
            for entry in json.loads(manifest.read_text()).get("screens", [])
            if entry["screenshot"] not in shot_now
        ]
    merged = sorted(kept + entries, key=lambda entry: entry["route"])
    manifest.write_text(json.dumps({"screens": merged}, indent=2) + "\n")
    print(
        f"\n{len(entries)} screenshot(s) captured; manifest describes "
        f"{len(merged)} in {OUT.relative_to(REPO)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
