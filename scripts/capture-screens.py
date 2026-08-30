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


def _now_commit_time() -> int:
    """The timestamp of HEAD, not the wall clock.

    Wall clock would make every capture look newer than every source, so the
    staleness check would never fire — which is the failure this whole file
    exists to prevent.
    """
    out = subprocess.run(
        ["git", "log", "-1", "--format=%ct"], cwd=REPO,
        capture_output=True, text=True, check=True,
    )
    return int(out.stdout.strip())


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
    OUT.mkdir(parents=True, exist_ok=True)
    captured_at = _now_commit_time()
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
            entries.append(
                {
                    "route": route,
                    "screenshot": shot,
                    "captured_at": captured_at,
                    "sources": SOURCES.get(route, []) + SHARED,
                }
            )
            print(f"  {shot:16} {route}")

        await browser.close()

    (OUT / "manifest.json").write_text(
        json.dumps({"screens": entries}, indent=2) + "\n"
    )
    print(f"\n{len(entries)} screenshots in {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
