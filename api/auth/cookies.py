"""HTTP-only cookie transport for both tokens (REQ-103)."""

from fastapi import Response

from api.config import get_settings

ACCESS_COOKIE = "bindery_access"
REFRESH_COOKIE = "bindery_refresh"
# The refresh cookie is only ever sent to the endpoints that rotate it.
REFRESH_COOKIE_PATH = "/api/auth"


def set_auth_cookies(response: Response, access_token: str, refresh_secret: str) -> None:
    settings = get_settings()
    response.set_cookie(
        ACCESS_COOKIE,
        access_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.jwt_access_ttl_minutes * 60,
        path="/",
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_secret,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.jwt_refresh_ttl_days * 24 * 60 * 60,
        path=REFRESH_COOKIE_PATH,
    )


def clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)
