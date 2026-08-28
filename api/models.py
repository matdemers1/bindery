"""The models Bindery will classify with, and what they cost.

A closed set rather than a free-text field, for two reasons that both bite in
practice. A typo'd model id fails *every* classification, and it fails at the
worker rather than at the form, so the feedback arrives hours later as a queue
full of dead letters. And the cost estimate on the health panel is meaningless
unless it knows which model produced the tokens.

Prices are per million tokens, published list rates. They are here to catch a
runaway loop — a bug that presents as a bill — not to reconcile an invoice, and
a stale number is still fine for that.

Choosing is a real trade-off rather than a "better/worse" ladder, so each entry
carries the sentence a person actually needs to decide.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelChoice:
    id: str
    name: str
    blurb: str
    input: float
    output: float
    cache_write: float
    cache_read: float

    def price(self, kind: str) -> float:
        return getattr(self, kind)


MODELS: tuple[ModelChoice, ...] = (
    ModelChoice(
        id="claude-opus-5",
        name="Opus 5",
        blurb=(
            "The most careful reader. Best on messy scans, handwriting and bundles "
            "that need splitting — and the most expensive by a wide margin."
        ),
        input=5.0, output=25.0, cache_write=6.25, cache_read=0.5,
    ),
    ModelChoice(
        id="claude-sonnet-5",
        name="Sonnet 5",
        blurb=(
            "A good default. Roughly a fifth of the cost of Opus and close to it on "
            "clean, typed documents — most of an ordinary household archive."
        ),
        input=3.0, output=15.0, cache_write=3.75, cache_read=0.3,
    ),
    ModelChoice(
        id="claude-haiku-4-5-20251001",
        name="Haiku 4.5",
        blurb=(
            "The cheapest, by a lot. Fine for titling and dating clean documents; "
            "expect to correct it more often on anything handwritten or unusual."
        ),
        input=1.0, output=5.0, cache_write=1.25, cache_read=0.1,
    ),
)

BY_ID = {choice.id: choice for choice in MODELS}
DEFAULT = MODELS[0].id


def is_valid(model_id: str) -> bool:
    return model_id in BY_ID


def pricing(model_id: str | None) -> ModelChoice:
    """Prices for a model, falling back to the most expensive one.

    Deliberately pessimistic: an unrecognised model — an older classification
    written before the set changed — is costed at the top rate, so the estimate
    errs towards alarming you rather than reassuring you.
    """
    return BY_ID.get(model_id or "", MODELS[0])
