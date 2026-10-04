"""RFC 9457 problem details, as the D3 App contract answers every refusal (CON-ADR-003).

Only the native contract routes use this: the web console reads FastAPI's `{"detail": …}` and has
no reason to change. A registered type is `https://d3cloud.io/problems/<name>`; a client acts on the
type, so the type — not the words — is the interface.
"""

from typing import Any

from fastapi.responses import JSONResponse

PROBLEM_BASE = "https://d3cloud.io/problems/"
MEDIA_TYPE = "application/problem+json"


def problem(
    status: int,
    name: str | None,
    title: str,
    *,
    detail: str | None = None,
    headers: dict[str, str] | None = None,
    **extensions: Any,
) -> JSONResponse:
    """A problem response. `name` is a registered type (`invalid_credentials`), or None for
    `about:blank` when no registered type fits."""
    body: dict[str, Any] = {
        "type": f"{PROBLEM_BASE}{name}" if name else "about:blank",
        "title": title,
        "status": status,
    }
    if detail:
        body["detail"] = detail
    body.update(extensions)
    return JSONResponse(body, status_code=status, media_type=MEDIA_TYPE, headers=headers)
