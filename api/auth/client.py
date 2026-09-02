"""Which address a request came from, as far as we can honestly tell.

One definition, because there were two — `api/routers/auth.py` and
`api/routers/accounts.py` each had a private copy, and both had the same bug.

Behind the Cloudflare tunnel every request arrives from the tunnel container, so
`request.client` names nothing useful. `CF-Connecting-IP` is set by the
Cloudflare edge and cannot be spoofed by the client *because* the tunnel is the
only route in — there is no published port on the origin (REQ-104).

The fallback is where the bug was. nginx sets
`X-Forwarded-For $proxy_add_x_forwarded_for`, which **appends** the peer it
actually saw to whatever the client sent. So in `a, b, c` the last element is
the one a proxy in this deployment wrote, and every element before it is
attacker-supplied text. Reading the *first* element meant a client could hand
over a fresh address per attempt — `X-Forwarded-For: 10.0.0.7`, then `10.0.0.8`
— and the per-IP half of `api/auth/throttle.py`, the half that actually stops a
password list, would never count two failures against the same key.

The per-IP counter is load-bearing rather than defence in depth (ADR-008), so
this reads the last element and nothing else.
"""

import logging

from fastapi import Request

log = logging.getLogger("bindery.auth")

_warned_about_missing_cf_header = False


def _warn_once_about_the_missing_edge_header() -> None:
    """Say it once, at warning, rather than never or forty thousand times.

    `CF-Connecting-IP` being absent means a request reached the origin by some
    route other than the tunnel, and the assumption the throttle rests on has
    stopped holding. That is worth exactly one line in the log — a silent
    fallback is how an assumption stops being true without anyone noticing.
    """
    global _warned_about_missing_cf_header
    if not _warned_about_missing_cf_header:
        _warned_about_missing_cf_header = True
        log.warning(
            "a request arrived with no CF-Connecting-IP header — the login "
            "throttle is falling back to the proxy chain, which is weaker. "
            "Check that the Cloudflare tunnel is still the only route in."
        )


def client_ip(request: Request) -> str | None:
    """The caller's address for throttling purposes, or None if we cannot tell."""
    edge = (request.headers.get("cf-connecting-ip") or "").strip()
    if edge:
        return edge

    _warn_once_about_the_missing_edge_header()

    forwarded = request.headers.get("x-forwarded-for", "")
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    if hops:
        # The last hop, never the first: everything before it is whatever the
        # client chose to send.
        return hops[-1]

    return request.client.host if request.client else None
