"""Every screen stays reachable from the shell (T-14.7, REQ-172).

Phase 14 moved five destinations out of the sidebar and into the account menu.
That is a good change and it is also exactly how a screen gets stranded: the
list gets shorter, the app looks tidier, and something is quietly unreachable
until somebody goes looking for it months later.

So this walks the router's own route table and the shell's own link tables, and
insists they agree. Both are read from source rather than restated here — a copy
of either would drift and keep passing.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "web" / "src" / "App.tsx"
SHELL = ROOT / "web" / "src" / "components" / "Shell.tsx"

# Reached from somewhere other than the navigation, by design.
NOT_IN_THE_SIDEBAR = {
    "/add": "the Add files button, which is deliberately more prominent than a row",
    "/ask": "a redirect to /",
    "/join": "an invitation link, arrived at from outside the app",
}


def routes() -> set[str]:
    """Top-level, non-parameterised routes from the router."""
    found = set()
    for match in re.finditer(r'path="(/[^"]*)"', APP.read_text()):
        path = match.group(1)
        if "*" in path or ":" in path:
            continue
        found.add(path)
    return found


def linked() -> set[str]:
    return set(re.findall(r'to:\s*"(/[^"]*)"', SHELL.read_text()))


def test_every_route_has_a_link_somewhere_in_the_shell():
    reachable = linked()
    orphans = sorted(
        route
        for route in routes()
        if route not in reachable
        and not any(route.startswith(prefix) for prefix in NOT_IN_THE_SIDEBAR)
    )
    assert not orphans, (
        "these screens exist and nothing links to them: "
        + ", ".join(orphans)
        + "\nAdd them to a group or to ACCOUNT_ITEMS in Shell.tsx, or record why "
        "they are reached another way in NOT_IN_THE_SIDEBAR."
    )


def test_the_guard_is_actually_looking_at_something():
    """A guard that silently examines nothing reads green forever."""
    assert len(routes()) >= 15, "the route parser has stopped matching App.tsx"
    assert len(linked()) >= 10, "the link parser has stopped matching Shell.tsx"


def test_no_link_points_at_a_route_that_does_not_exist():
    """The other direction: a tidy-up that renames a route and leaves the link."""
    known = routes()
    broken = sorted(target for target in linked() if target not in known)
    assert not broken, f"the sidebar links to routes that do not exist: {broken}"


@pytest.mark.parametrize("path", ["/trust", "/pipeline"])
def test_the_daily_group_still_holds_what_changes_on_its_own(path):
    """Keep narrowed to the two screens that change without you touching them.
    If either drifts out of the sidebar, the archive stops reporting itself."""
    shell = SHELL.read_text()
    keep = shell[shell.index('title: "Keep"') : shell.index("const ACCOUNT_ITEMS")]
    assert f'to: "{path}"' in keep
