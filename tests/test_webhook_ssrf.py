"""The webhook is checked where the connection is made, not only where it is saved.

`api/routers/settings.py` refuses a literal loopback or link-local URL and
deliberately does not resolve a name — resolving at save time proves nothing
about what the name answers at 3am. That leaves two shapes it cannot see: a
DNS name pointing at the metadata service, and an ordinary host that answers
302 to one. Both are checked here, at send time.

RFC1918 stays allowed throughout: a self-hosted notifier on the LAN is the use
case this feature exists for.
"""

import urllib.error
import urllib.request

import pytest

from api import notify


def test_a_name_that_resolves_to_link_local_is_refused(monkeypatch) -> None:
    """The shape the string check cannot see."""
    monkeypatch.setattr(
        notify.socket, "getaddrinfo",
        lambda host, port, *a, **k: [(2, 1, 6, "", ("169.254.169.254", 0))],
    )
    with pytest.raises(ValueError, match="metadata service"):
        notify._refuse_unroutable("https://notifier.example.test/hook")


def test_a_name_that_resolves_to_loopback_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(
        notify.socket, "getaddrinfo",
        lambda host, port, *a, **k: [(2, 1, 6, "", ("127.0.0.1", 0))],
    )
    with pytest.raises(ValueError, match="talking to"):
        notify._refuse_unroutable("https://notifier.example.test/hook")


def test_a_private_address_is_still_allowed(monkeypatch) -> None:
    """A self-hosted ntfy on the LAN is the whole point of the feature."""
    monkeypatch.setattr(
        notify.socket, "getaddrinfo",
        lambda host, port, *a, **k: [(2, 1, 6, "", ("192.168.1.50", 0))],
    )
    notify._refuse_unroutable("http://ntfy.lan/hook")


def test_every_resolved_address_is_checked_not_just_the_first(monkeypatch) -> None:
    """A name may answer with several records; one bad one is enough."""
    monkeypatch.setattr(
        notify.socket, "getaddrinfo",
        lambda host, port, *a, **k: [
            (2, 1, 6, "", ("192.168.1.50", 0)),
            (2, 1, 6, "", ("169.254.169.254", 0)),
        ],
    )
    with pytest.raises(ValueError):
        notify._refuse_unroutable("https://notifier.example.test/hook")


def test_the_opener_refuses_to_follow_a_redirect() -> None:
    """A redirect is a second URL nobody validated."""
    handler = notify._NoRedirects()
    request = urllib.request.Request("https://notifier.example.test/hook")
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(
            request, None, 302, "Found", {}, "http://169.254.169.254/latest/meta-data/"
        )


def test_send_reports_the_refusal_rather_than_raising(monkeypatch) -> None:
    """Invariant: a notifier must never take down the thing it reports on."""
    monkeypatch.setattr(
        notify.socket, "getaddrinfo",
        lambda host, port, *a, **k: [(2, 1, 6, "", ("169.254.169.254", 0))],
    )
    notifier = notify.Notifier(webhook_url="https://notifier.example.test/hook")
    alert = notify.Alert(
        code="stalled", severity="warning", message="the queue has stalled", detail={}
    )
    delivery = notifier.send(alert)
    assert delivery.sent is False
    assert "metadata service" in (delivery.reason or "")
