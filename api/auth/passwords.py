"""Argon2 password hashing and the password policy (REQ-103, REQ-137).

The policy exists because Cloudflare Access is coming off (ADR-008) and this
login page becomes internet-facing. It is checked at *set* time, never at login:
tightening the rules must not lock out someone whose existing password no longer
satisfies them.

The common-password list is bundled rather than fetched. Checking a password
against a remote breach service means deriving something from it and sending
that derivative off the host, which is the exact opposite of what this archive
is for — so the trade is a much smaller list, kept locally, and a length
requirement that does most of the work anyway.
"""

from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()

# Long enough that the common-password list stops being the main defence. NIST
# recommends length over composition rules, and composition rules mostly produce
# `Password1!`.
MIN_LENGTH = 12
MAX_LENGTH = 256  # Argon2 will hash anything; a 10 MB password is a DoS.

_COMMON_PATH = Path(__file__).with_name("common_passwords.txt")
_common: set[str] | None = None

_decoy: str | None = None


class WeakPassword(ValueError):
    """Refused at set time, with a reason a person can act on."""


def _common_passwords() -> set[str]:
    global _common
    if _common is None:
        try:
            _common = {
                line.strip().lower()
                for line in _COMMON_PATH.read_text().splitlines()
                if line.strip() and not line.startswith("#")
            }
        except OSError:
            _common = set()
    return _common


def validate_password(password: str, *, email: str | None = None) -> None:
    """Raise `WeakPassword` if this password may not be set."""
    if len(password) < MIN_LENGTH:
        raise WeakPassword(f"Use at least {MIN_LENGTH} characters.")
    if len(password) > MAX_LENGTH:
        raise WeakPassword(f"Use at most {MAX_LENGTH} characters.")
    if password.lower() in _common_passwords():
        raise WeakPassword(
            "That is one of the most commonly used passwords. Pick another."
        )
    if email:
        local = email.split("@")[0].strip().lower()
        if local and local in password.lower():
            raise WeakPassword("Do not put your email address in your password.")


def decoy_hash() -> str:
    """A real Argon2 hash to verify against when the account does not exist.

    Computed once, of a value nothing can match, so that a login attempt for an
    unknown address costs the same as one for a known address.
    """
    global _decoy
    if _decoy is None:
        _decoy = _hasher.hash("decoy-for-constant-time-authentication")
    return _decoy


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
