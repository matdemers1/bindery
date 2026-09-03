"""`api/schemas.py` has to be able to say what is in it (CR-090).

The module is the highest-contention file in the repository — every feature
touches it — and it carried two indexes that had quietly stopped being true. The
banners named build phases, so three regions had landed under the wrong one; and
`__all__` sat two-thirds of the way up the file listing 78 of 124 models, with
every model defined after it silently absent.

An index nobody maintains is worse than no index, because readers still trust
it. So this asserts the one that can be asserted.
"""

import re
from pathlib import Path

import api.schemas as schemas

SOURCE = Path(schemas.__file__).read_text()


def test_all_lists_every_model_and_nothing_else() -> None:
    defined = sorted({name for name in re.findall(r"^class (\w+)\(", SOURCE, re.M)})
    declared = sorted(schemas.__all__)

    missing = [name for name in defined if name not in declared]
    assert not missing, (
        "these models are defined in api/schemas.py and absent from __all__ — "
        "add them to the list at the bottom of the file:\n  " + "\n  ".join(missing)
    )

    stale = [name for name in declared if name not in defined]
    assert not stale, (
        "__all__ names models that no longer exist:\n  " + "\n  ".join(stale)
    )

    assert declared == sorted(declared), "__all__ is sorted; keep it that way"
    for name in declared:
        assert hasattr(schemas, name), f"{name} is exported but not importable"


def test_the_section_banners_are_domains_rather_than_phases() -> None:
    """The scheme that broke down, kept from coming back.

    "Which phase added photos?" is not a question a stranger can answer, and it
    was the question this file's index required them to answer before they could
    find anything.
    """
    banners = re.findall(r"^# (.+)$", SOURCE, re.M)
    phase_named = [
        line for line in banners if re.match(r"^(Phase \d|.*\(Phase \d)", line.strip())
    ]
    assert not phase_named, (
        "section banners name a build phase rather than a domain:\n  "
        + "\n  ".join(phase_named)
    )
    # And it is still indexed at all — a file this size with no banners is the
    # other failure.
    assert len(banners) >= 15, f"only {len(banners)} section banners found"
