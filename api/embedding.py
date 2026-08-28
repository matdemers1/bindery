"""Document embeddings for neighbour retrieval (REQ-062).

Behind a provider interface, like every other external-service boundary in the
project, so swapping the implementation is a configuration change rather than a
rewrite.

**The default is deliberately lexical, not semantic** — see ADR-007. These
embeddings exist to answer one question: *which already-filed documents look
like this one, so their tags can be offered as candidates?* The neighbours worth
finding are other statements from the same insurer, other invoices on the same
template — documents that share literal vocabulary and structure. Hashed term
frequency captures that well, runs in microseconds on CPU, needs no GPU, and
sends nothing to a second third party. Semantic search was declined outright
(ADR-004), so nothing else in the system needs more.

The cost is real and named: paraphrase similarity is not captured. A hosted or
local transformer model slots in behind `EmbeddingProvider` when that matters.
"""

import hashlib
import math
import re
from collections import Counter
from typing import Protocol

# Small enough that the HNSW index stays cheap, wide enough that hash collisions
# between distinct terms stay rare at household-archive vocabulary sizes.
EMBEDDING_DIMENSIONS = 512

# Fold digits to a marker: an account number is a strong signal that two
# documents come from the same correspondent, but the literal digits differ
# between statements and would otherwise look like unrelated vocabulary.
_TOKEN = re.compile(r"[^\W\d_]+|\d+")
_MAX_TOKENS = 4000


class EmbeddingProvider(Protocol):
    """The swap point. See ADR-007."""

    name: str
    dimensions: int

    def embed(self, text: str) -> list[float]: ...


def _tokens(text: str) -> list[str]:
    out: list[str] = []
    for match in _TOKEN.finditer(text.casefold()):
        token = match.group()
        # A run of digits carries structure but not identity.
        out.append("#num" if token.isdigit() else token)
        if len(out) >= _MAX_TOKENS:
            break
    return out


def _bucket(token: str, dimensions: int) -> int:
    # Stable across processes and restarts, unlike hash() — an embedding that
    # changes between runs would silently corrupt every stored vector.
    digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") % dimensions


class HashedLexicalEmbedding:
    """Hashed sublinear term frequency, L2-normalised.

    Sublinear (`1 + log tf`) so a word repeated forty times on a form does not
    drown out the words that identify it, and L2-normalised so cosine distance
    is comparable between a one-page bill and a forty-page policy.
    """

    name = "hashed-lexical-v1"

    def __init__(self, dimensions: int = EMBEDDING_DIMENSIONS) -> None:
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        counts = Counter(_tokens(text))
        vector = [0.0] * self.dimensions
        for token, count in counts.items():
            vector[_bucket(token, self.dimensions)] += 1.0 + math.log(count)

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            # An empty or unreadable document. A zero vector has no cosine
            # distance to anything, so it is stored as-is and simply never
            # becomes anyone's neighbour.
            return vector
        return [value / norm for value in vector]


_default = HashedLexicalEmbedding()


def get_embedding_provider() -> EmbeddingProvider:
    return _default
