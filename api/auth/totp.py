"""TOTP, implemented against RFC 6238 rather than pulled in as a dependency.

The whole algorithm is thirty lines and the standard library already has the
HMAC and the base32. A dependency here would be a supply-chain surface on the
authentication path of an archive holding other people's medical records, in
exchange for saving those thirty lines.

Two properties the tests pin down, because both are easy to get subtly wrong and
neither announces itself:

**A window either side.** Clocks drift and people type slowly, so the code from
the previous and next 30-second step are accepted. One step either way is the
usual compromise: three codes valid at any moment out of a million.

**Codes are single-use within their window.** Without that, a code shoulder-
surfed or captured from a phishing page stays valid for up to 90 seconds, which
is ample. `last_used_step` on the user is what closes it.
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

import segno

DIGITS = 6
STEP_SECONDS = 30
# How many 30-second steps either side of now are accepted. One is the usual
# compromise between clock drift and the size of the valid set.
WINDOW = 1
SECRET_BYTES = 20  # 160 bits, the RFC 4226 recommendation


def new_secret() -> str:
    """A fresh base32 secret, in the form authenticator apps expect."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode().rstrip("=")


def provisioning_uri(secret: str, *, email: str, issuer: str = "Bindery") -> str:
    """The `otpauth://` URI a QR code encodes.

    Built here rather than by a library so the label and issuer are exactly what
    the account holder will see in their authenticator, which is the one part of
    enrolment they cannot change afterwards.
    """
    label = quote(f"{issuer}:{email}", safe="")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
        f"&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"
    )


def _code_for_step(secret: str, step: int) -> str:
    # Base32 without padding is what every authenticator shows; put it back.
    padding = "=" * (-len(secret) % 8)
    key = base64.b32decode(secret + padding, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10**DIGITS)).zfill(DIGITS)


def current_step(at: float | None = None) -> int:
    return int((at if at is not None else time.time()) // STEP_SECONDS)


def verify(secret: str, code: str, *, after_step: int | None = None) -> int | None:
    """Return the step the code belongs to, or None.

    The step is returned rather than a bool so the caller can record it and
    refuse the same code twice — a code accepted for 90 seconds is a code worth
    capturing. `after_step` rejects anything at or before a step already used.
    """
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != DIGITS:
        return None

    now = current_step()
    for step in range(now - WINDOW, now + WINDOW + 1):
        if after_step is not None and step <= after_step:
            continue
        # Constant-time: a length-safe comparison of two six-digit strings is
        # cheap, and a timing difference here leaks the code a digit at a time.
        if hmac.compare_digest(_code_for_step(secret, step), code):
            return step
    return None


def qr_svg(uri: str) -> str:
    """The provisioning URI as an inline SVG, dark modules on white.

    White rather than transparent: authenticator cameras read contrast, and a
    dark theme behind a transparent code is a code nobody can scan. Scaled by
    its viewBox, so the page decides how large it is drawn.
    """
    return segno.make(uri, error="m").svg_inline(
        scale=1, border=2, dark="#101117", light="#ffffff", omitsize=True
    )
