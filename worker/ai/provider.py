"""The AI swap point (REQ-044, ADR-003).

Everything the pipeline knows about classification is this protocol and the
shapes below. A local or hybrid backend is a configuration change, not a
rewrite — which is the reversal path for the accepted privacy trade in ADR-003:
OCR text of medical, identity and financial documents leaving the network is a
deliberate decision, and this interface is what keeps it reversible.

The contract deliberately separates **reuse from invention**. `existing_ids`
resolve deterministically by id and never pass through normalisation,
translation, or fuzzy matching, so an exact match cannot be silently corrupted.
`new_names` are quarantined for review before entering the taxonomy.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, Field


class TaxonomyChoice(BaseModel):
    """Reuse or invention — never both, never a bare string.

    A single `correspondent: str` field would make "GEICO" and "Geico Insurance"
    indistinguishable from the caller's side, which is exactly how taxonomies
    drift. Splitting the field makes reuse the structurally easier answer.
    """

    existing_id: str | None = Field(
        default=None, description="The id of an existing entry, copied exactly from the candidates."
    )
    new_name: str | None = Field(
        default=None, description="A new name, only when no candidate fits."
    )


class TaxonomyChoices(BaseModel):
    existing_ids: list[str] = Field(
        default_factory=list, description="Ids of existing entries, copied exactly."
    )
    new_names: list[str] = Field(
        default_factory=list, description="New names, only where no candidate fits."
    )


class Evidence(BaseModel):
    """Where a field came from. A classification without evidence is a bug."""

    field: str = Field(description="The field this justifies, e.g. document_date.")
    page: int = Field(description="Page number within the document, 1-indexed.")
    snippet: str = Field(description="The exact sentence from the page that justifies it.")


class ClassificationResult(BaseModel):
    """The strict response contract (ADR-003)."""

    title: str = Field(
        description="'Correspondent - Type - Identifier', at most 12 words, "
        "account numbers masked to the last 4 digits."
    )
    summary: str = Field(description="One or two sentences describing what this document is.")
    document_date: str | None = Field(
        default=None,
        description="The document's own date as YYYY-MM-DD, or null. Never guess.",
    )
    language: str = Field(default="en", description="ISO 639-1 code.")
    correspondent: TaxonomyChoice = Field(default_factory=TaxonomyChoice)
    document_type: TaxonomyChoice = Field(default_factory=TaxonomyChoice)
    tags: TaxonomyChoices = Field(default_factory=TaxonomyChoices)
    confidence: dict[str, float] = Field(
        default_factory=dict,
        description="Per-field confidence 0-1. Displayed to the user; not used to decide filing.",
    )
    evidence: list[Evidence] = Field(default_factory=list)
    # Only meaningful when the document was pre-identified as a known form.
    extracted_fields: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True)
class Candidate:
    """One entry offered to the model, with the id it must copy back."""

    id: str
    name: str
    # How many of the document's neighbours use this. Signals likelihood, which
    # dumping the whole taxonomy cannot.
    neighbour_count: int = 0


@dataclass
class ClassificationRequest:
    document_id: str
    pages: list[tuple[int, str]]
    correspondents: list[Candidate] = field(default_factory=list)
    document_types: list[Candidate] = field(default_factory=list)
    tags: list[Candidate] = field(default_factory=list)
    known_form_code: str | None = None
    known_form_fields: list[str] = field(default_factory=list)


@dataclass
class ClassificationResponse:
    result: ClassificationResult
    model: str
    prompt_version: str
    usage: dict[str, Any]
    raw_request: dict[str, Any]
    raw_response: dict[str, Any]


class BoundaryVerdict(BaseModel):
    """Whether a candidate seam really starts a new document."""

    page: int = Field(description="The page the candidate boundary is at.")
    starts_new_document: bool = Field(
        description="True if a different document begins on this page."
    )
    reason: str = Field(description="One short sentence, citing what you saw.")


class BoundaryConfirmation(BaseModel):
    verdicts: list[BoundaryVerdict] = Field(default_factory=list)


@dataclass
class BoundaryWindow:
    """A candidate seam, with the pages either side of it."""

    page: int
    reasons: list[str]
    # (page_number, text) for a few pages before and after the candidate.
    before: list[tuple[int, str]] = field(default_factory=list)
    after: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class BoundaryRequest:
    source_file_id: str
    windows: list[BoundaryWindow]


class AIProviderError(RuntimeError):
    """The provider could not produce a valid result.

    Raised loudly and retried. A malformed response is **never** silently
    accepted or partially applied (REQ-045): half a classification looks like a
    confident answer, and a confidently wrong answer is the kill criterion.
    """


class ProviderUnavailableError(AIProviderError):
    """The provider is unreachable or unconfigured.

    Distinct from a malformed response because the handling differs: this is
    retried with backoff indefinitely, and the document stays fully searchable
    in the meantime (REQ-055).
    """


class AIProvider(Protocol):
    name: str
    model: str
    prompt_version: str

    def available(self) -> bool: ...

    async def classify(self, request: ClassificationRequest) -> ClassificationResponse: ...

    async def confirm_boundaries(self, request: BoundaryRequest) -> BoundaryConfirmation:
        """Rule on seams the heuristics could not settle (REQ-035).

        Only ambiguous candidates reach here — cheap signals do the easy work,
        so the expensive one only sees the cases that need judgement.
        """
        ...
