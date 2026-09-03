"""Telling you when the pipeline stopped (T-8.3, REQ-110).

The health panel only helps if someone looks at it, and the whole failure mode
being defended against is one you would never think to look for. So a permanent
failure or a stall has to leave the building and arrive on a phone.

Delivery is an adapter, like the AI provider: a webhook URL is the default
because ntfy, Pushover, Gotify, Slack and Discord all accept one, and a
self-hosted archive should not require an account with anybody.

Two properties matter more than the transport:

**Deduplication.** A stalled pipeline is stalled continuously. Notifying every
minute trains you to ignore the notification, which is worse than not sending
it. Each alert code is sent at most once per cooldown window, and re-sent when
the condition recurs after clearing.

**Notification failure is never pipeline failure.** If the webhook is down, that
is logged and the archive carries on. A missed notification is bad; an ingest
that stopped because a notification failed is worse.
"""

import ipaddress
import json
import logging
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from api.health_panel import Alert

log = logging.getLogger("bindery.notify")

# Long enough that a stall does not become a stream, short enough that a
# recurring problem is not silently swallowed for a day.
COOLDOWN = timedelta(hours=6)

SETTING_KEY = "notify_webhook_url"


@dataclass
class Delivery:
    code: str
    sent: bool
    reason: str | None = None


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect is a second URL nobody validated.

    Settings refuses a webhook pointing at loopback or link-local, but an
    ordinary https host answering 302 to `http://169.254.169.254/...` reaches
    it anyway — and this POST carries no secret but does prove what the
    archive can see. Following is not worth that; a real notifier does not
    need it.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, f"refusing redirect to {newurl}", headers, fp
        )


_OPENER = urllib.request.build_opener(_NoRedirects)


def _refuse_unroutable(url: str) -> None:
    """Resolve the host and refuse what settings could only refuse literally.

    Settings deliberately does not resolve — a name proves nothing at save
    time — so the name is checked here, where the connection is about to be
    made. RFC1918 stays allowed on purpose: a self-hosted notifier on the LAN
    is the whole use case.
    """
    host = (urlparse(url).hostname or "").strip("[]")
    if not host:
        raise ValueError(f"{url!r} names no host")
    try:
        resolved = socket.getaddrinfo(host, None)
    except OSError as error:
        raise ValueError(f"{host} did not resolve: {error}") from error

    for _family, _type, _proto, _canon, sockaddr in resolved:
        address = ipaddress.ip_address(sockaddr[0])
        if (
            address.is_loopback
            or address.is_link_local
            or address.is_unspecified
            or address.is_multicast
            or address.is_reserved
        ):
            raise ValueError(
                f"{host} resolves to {address}, which is the host talking to "
                "itself or its metadata service, never a webhook"
            )


class Notifier:
    """Sends alerts, at most one per code per cooldown window."""

    def __init__(self, webhook_url: str | None, *, cooldown: timedelta = COOLDOWN) -> None:
        self.webhook_url = (webhook_url or "").strip()
        self.cooldown = cooldown
        self._last_sent: dict[str, datetime] = {}

    def should_send(self, code: str, *, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        last = self._last_sent.get(code)
        return last is None or now - last >= self.cooldown

    def clear(self, code: str) -> None:
        """The condition resolved; the next occurrence is news again."""
        self._last_sent.pop(code, None)

    def send(self, alert: Alert, *, now: datetime | None = None) -> Delivery:
        now = now or datetime.now(UTC)
        if not self.webhook_url:
            return Delivery(alert.code, False, "no webhook configured")
        if not self.should_send(alert.code, now=now):
            return Delivery(alert.code, False, "within cooldown")

        payload = {
            "title": f"Bindery: {alert.code.replace('_', ' ')}",
            "message": alert.message,
            "priority": 5 if alert.severity == "critical" else 3,
            "tags": ["warning" if alert.severity == "warning" else "rotating_light"],
            "detail": alert.detail,
        }
        request = urllib.request.Request(
            self.webhook_url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            _refuse_unroutable(self.webhook_url)
            with _OPENER.open(request, timeout=10) as response:
                ok = 200 <= response.status < 300
        except (urllib.error.URLError, OSError, ValueError) as error:
            # Never raise. A notifier that can take the pipeline down with it
            # is a worse problem than the one it was added to report.
            log.error("notification for %s failed: %s", alert.code, error)
            return Delivery(alert.code, False, str(error))

        if ok:
            self._last_sent[alert.code] = now
        return Delivery(alert.code, ok, None if ok else "webhook rejected the request")

    def dispatch(self, alerts: list[Alert], *, now: datetime | None = None) -> list[Delivery]:
        """Send what is new, and forget what has resolved.

        Only critical alerts leave the building. A handful of failed jobs is
        something to see on the panel next time you look; a stopped pipeline is
        something to be told about.
        """
        now = now or datetime.now(UTC)
        active = {alert.code for alert in alerts}
        for code in list(self._last_sent):
            if code not in active:
                self.clear(code)
        return [
            self.send(alert, now=now) for alert in alerts if alert.severity == "critical"
        ]
