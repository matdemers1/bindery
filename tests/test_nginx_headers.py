"""Every location that sets a header re-includes the security headers.

nginx's `add_header` inheritance is a trap with no error message: **a level
that declares any `add_header` inherits none from its parent.** Adding one
`Cache-Control` to a `location` block silently dropped Content-Security-Policy,
HSTS, X-Frame-Options and four more from `index.html` and every asset — on the
login page of an origin that faces the open internet, since Cloudflare Access
was removed (ADR-008).

Nothing caught it. This is a structural check rather than a live request
because the config is what ships and a live check needs a running nginx, but
the trap is structural: the defect is always "this block sets a header and did
not re-include".
"""

import re
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parent.parent / "infra"
CONF = INFRA / "nginx.conf"
SNIPPET = INFRA / "security-headers.conf"

REQUIRED = [
    "Content-Security-Policy",
    "Strict-Transport-Security",
    "X-Frame-Options",
    "X-Content-Type-Options",
    "Referrer-Policy",
    "Cross-Origin-Opener-Policy",
    "Permissions-Policy",
]


def _location_blocks(text: str) -> dict[str, str]:
    """Each `location <match> { ... }` body, by its match expression."""
    blocks: dict[str, str] = {}
    for match in re.finditer(r"location\s+([^\s{]+)\s*\{", text):
        depth, i = 1, match.end()
        while depth and i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        blocks[match.group(1)] = text[match.end() : i - 1]
    return blocks


def test_the_snippet_carries_every_required_header() -> None:
    body = SNIPPET.read_text()
    missing = [h for h in REQUIRED if f"add_header {h} " not in body]
    assert not missing, f"the security-headers snippet no longer sets: {missing}"
    # `always`, or nginx omits them on error responses — the pages an attacker
    # is most likely to see.
    for line in body.splitlines():
        if line.strip().startswith("add_header "):
            assert line.rstrip().rstrip(";").endswith("always"), line


def test_the_server_includes_the_snippet() -> None:
    assert "include /etc/nginx/security-headers.conf;" in CONF.read_text()


@pytest.mark.parametrize("name", ["/", "/assets/"])
def test_a_location_that_sets_a_header_reincludes_them(name: str) -> None:
    block = _location_blocks(CONF.read_text())[name]
    assert "add_header" in block, f"{name} no longer sets a header; drop it from this test"
    assert "include /etc/nginx/security-headers.conf;" in block, (
        f"`location {name}` sets its own header, so nginx gives it *none* of the "
        "server's. Re-include /etc/nginx/security-headers.conf inside this block."
    )


def test_no_location_sets_a_header_without_reincluding() -> None:
    """The general form, so a new block cannot reintroduce this silently."""
    offenders = [
        name
        for name, block in _location_blocks(CONF.read_text()).items()
        if "add_header" in block
        and "include /etc/nginx/security-headers.conf;" not in block
    ]
    assert not offenders, (
        f"these location blocks set a header and inherit none: {offenders}. "
        "nginx drops every parent header at any level that declares one."
    )


def test_the_image_ships_the_snippet() -> None:
    dockerfile = (INFRA / "Dockerfile.web").read_text()
    assert "security-headers.conf /etc/nginx/security-headers.conf" in dockerfile, (
        "nginx.conf includes the snippet, so the image must carry it or the "
        "container fails to start"
    )
