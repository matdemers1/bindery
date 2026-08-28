"""Text helpers shared by both images.

Lives in `api/` because that is the only package both the api and the worker
carry, and the layering rule runs one way: `worker` may import `api`, never the
reverse (see CLAUDE.md).
"""

import re
import unicodedata


def slugify(name: str) -> str:
    """A stable, ascii, url-safe key for a taxonomy name.

    Used to derive tag and correspondent slugs. Note that slugs are *not* how
    reuse is resolved — that is always by id — so a collision here is a cosmetic
    problem, never a merge.
    """
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-") or "untitled"
