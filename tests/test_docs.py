"""Documentation that cannot quietly rot (T-11.3, T-11.9, REQ-150).

The operator's requirement was *"going forward, we'll have to update that with
every feature that we change"* — which is precisely the thing nobody does.
Documentation rots silently: nothing fails, the guide is simply a year out of
date and quietly misleading, which is worse than no guide at all.

So the things that can be checked are checked here, and the build fails on them:
a screen with no guide, a guide for a screen that no longer exists, a screenshot
that is missing, and a screenshot older than the code it depicts.

What cannot be checked is whether the prose is *true*. Nothing here pretends to.
"""

import json
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GUIDES_FILE = REPO / "web" / "public" / "help" / "guides.json"
SCREENS = REPO / "web" / "public" / "help" / "screens"
APP = REPO / "web" / "src" / "App.tsx"

# Routes a person never navigates to on purpose, or that are a detail view of
# something already documented. Each needs a reason, so that "add it to the
# exemptions" is a decision rather than a reflex.
UNDOCUMENTED_ON_PURPOSE = {
    "/ask": "a redirect kept so old links still work",
    "*": "the catch-all",
    "/join/*": "an invitation link, and the join page explains itself",
    "/document/:documentId": "redirects into the viewer",
    "/document/:documentId/page/:pageNumber": "the viewer, reached from a result",
    "/file/:fileId": "redirects into the viewer",
    "/file/:fileId/page/:pageNumber": "the viewer, reached from a result",
    "/file/:fileId/segments": "reached from the viewer, and explained there",
    "/help": "the guides themselves",
    "/libraries": "a household feature; documented when households are",
}


def _guides() -> dict:
    return json.loads(GUIDES_FILE.read_text())


def _routes_in_app() -> set[str]:
    """Every `<Route path="…">` the application declares."""
    return set(re.findall(r'<Route\s+path="([^"]+)"', APP.read_text()))


def test_every_screen_has_a_guide() -> None:
    """Adding a route without documenting it fails the build (REQ-150)."""
    documented = {guide["route"] for guide in _guides()["guides"]}
    missing = sorted(
        route
        for route in _routes_in_app()
        if route not in documented and route not in UNDOCUMENTED_ON_PURPOSE
    )
    assert not missing, (
        "these screens have no guide in docs/help/guides.json. Write one, or add "
        "it to UNDOCUMENTED_ON_PURPOSE with a reason:\n  " + "\n  ".join(missing)
    )


def test_no_guide_describes_a_screen_that_is_gone() -> None:
    """The other direction, which rots more quietly: a guide for a screen that
    was removed reads as current until somebody follows the link."""
    routes = _routes_in_app()
    stale = sorted(
        guide["route"] for guide in _guides()["guides"] if guide["route"] not in routes
    )
    assert not stale, f"guides describe screens that no longer exist: {stale}"


def test_the_exemptions_are_all_still_real_routes() -> None:
    """An exemption for a route that no longer exists hides the next screen that
    happens to be named the same thing."""
    routes = _routes_in_app()
    orphaned = sorted(route for route in UNDOCUMENTED_ON_PURPOSE if route not in routes)
    assert not orphaned, f"exempted routes that no longer exist: {orphaned}"


def test_every_guide_has_a_screenshot_on_disk() -> None:
    """A guide whose picture 404s is worse than one with no picture."""
    missing = [
        guide["screenshot"]
        for guide in _guides()["guides"]
        if not (SCREENS / guide["screenshot"]).is_file()
    ]
    assert not missing, (
        "no screenshot for: " + ", ".join(missing) + "\nRun `make screenshots`."
    )


def test_no_screenshot_is_older_than_the_screen_it_shows() -> None:
    """The check that makes this survive contact with the next six months.

    A screenshot does not announce that it is out of date — it is simply a
    picture of an application that no longer looks like that. Recording which
    commit last changed a screen's source, and comparing it against the same
    question asked now, turns silent rot into a failing build.

    A commit *hash*, not a timestamp. The first version compared timestamps and
    fired the moment the screenshots were committed, because that commit is
    newer than everything in it — every capture immediately invalidated itself.
    Committing a picture does not change the code it is a picture of, so a hash
    still matches; changing the screen is what stops it matching.

    Deliberately *not* a pixel comparison either. Antialiasing and font hinting
    differ between machines, so that check would fail for reasons unrelated to
    the documentation and would be switched off within a month.
    """
    manifest_file = SCREENS / "manifest.json"
    # Not a skip. `make screenshots` writes this file and it is committed; if it
    # is missing, the documentation has no captured state at all, and the guard
    # that was going to notice said nothing. (`web/public` is bind-mounted into
    # the test image precisely so this is readable.)
    assert manifest_file.is_file(), (
        f"{manifest_file.relative_to(REPO)} is missing, so the staleness guard "
        "has nothing to compare. Run `make screenshots` and commit the result."
    )

    manifest = json.loads(manifest_file.read_text())
    screens = manifest["screens"]
    stale: list[str] = []
    resolved = 0
    orphaned: list[str] = []
    for entry in screens:
        sources = entry.get("sources") or []
        # An entry with no sources is an entry the check cannot make. It used to
        # `continue` in silence, which is how a manifest could go green while
        # answering nothing at all.
        assert sources, (
            f"{entry['screenshot']} lists no sources, so nothing can say whether "
            "it is out of date"
        )
        orphaned.extend(
            f"{entry['screenshot']} → {source}"
            for source in sources
            if not (REPO / source).exists()
        )
        # Test files are excluded from the comparison. A `.test.tsx` beside a
        # screen cannot change what the screen looks like, and asking for a
        # re-capture because a fixture gained a field trains people to run
        # `make screenshots` without looking at the result — which is the one
        # habit that makes this check worthless. Pathspecs, so the exclusion
        # travels with each source rather than being reapplied per directory.
        current = subprocess.run(
            [
                "git", "log", "-1", "--format=%H", "--",
                *sources,
                ":(exclude)*.test.tsx", ":(exclude)*.test.ts",
                ":(exclude)*.spec.tsx", ":(exclude)*.spec.ts",
            ],
            cwd=REPO, capture_output=True, text=True, check=False,
        ).stdout.strip()
        if not current:
            continue
        resolved += 1
        if current != entry.get("source_commit"):
            stale.append(entry["screenshot"])

    # The two ways this guard falls silent, both routine and both previously
    # invisible: `sources` is a hand-maintained list of directories, so a
    # component moved during a refactor orphans an entry without changing a
    # single test outcome; and with no git history (a shallow clone, or the
    # `.git` bind-mount gone from infra/docker-compose.yml) `git log` answers
    # nothing for every entry and `stale` stays empty forever.
    assert not orphaned, (
        "these manifest entries point at source paths that no longer exist, so "
        "the staleness check silently skips them:\n  " + "\n  ".join(orphaned)
    )
    assert resolved == len(screens), (
        f"git answered for only {resolved} of {len(screens)} screens — the "
        "staleness check is inspecting less than it claims. Either the sources "
        "are untracked, or this run has no git history to ask (check that "
        "`../.git` is still bind-mounted into the test service)."
    )

    assert not stale, (
        "these screenshots are older than the code they show:\n  "
        + "\n  ".join(stale)
        + "\nRun `make screenshots` and commit the result."
    )


def test_the_faq_answers_the_questions_that_matter_most() -> None:
    """Not a style check. These are the promises the archive makes to people who
    are handing over medical and financial records, and a FAQ that quietly stops
    making one of them is a change worth noticing.
    """
    questions = " ".join(entry["question"].lower() for entry in _guides()["faq"])
    answers = " ".join(entry["answer"].lower() for entry in _guides()["faq"])

    assert "read my documents" in questions, "who can read my documents"
    assert "deleted" in questions, "does anything get deleted"
    assert "get my documents out" in questions, "can I leave"
    assert "anthropic" in questions, "what leaves the machine"
    # And the answers have to still say the right thing.
    assert "no." in answers
    assert "never modified" in answers
