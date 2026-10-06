#!/usr/bin/env python3
"""Write a pytest run's skips and expected failures to a CI job summary.

    python3 scripts/ci_test_summary.py <junit.xml> [report.md ...] >> "$GITHUB_STEP_SUMMARY"

A skip in a green run of hundreds is invisible: it is one digit in a count
nobody reads, and it is how the R-01 OCR gate went three phases looking like a
pass (BND-CR-037). This puts every skipped and xfailed test on the run's summary
page by name, with the reason it gave, then appends any Markdown reports the run
wrote (the synthetic OCR figure). Standard library only: it runs on the bare
runner, after the containers are gone.
"""

import sys
from pathlib import Path
from xml.etree import ElementTree


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def summarize(junit: Path) -> str:
    if not junit.is_file():
        return (
            f"> [!WARNING]\n> No test results at `{junit}`: "
            "the run did not get far enough to write them.\n"
        )

    root = ElementTree.parse(junit).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in suites:
        for key in counts:
            counts[key] += int(suite.get(key, 0))

    skipped: list[tuple[str, str, str]] = []
    for case in root.iter("testcase"):
        name = f"{case.get('classname', '')}::{case.get('name', '')}"
        for element in case.findall("skipped"):
            # pytest writes an xfail as a skip whose type is pytest.xfail.
            kind = "xfail" if element.get("type") == "pytest.xfail" else "skipped"
            reason = element.get("message") or (element.text or "")
            skipped.append((kind, name, reason))

    lines = [
        f"### Tests: {counts['tests']} run, {counts['failures']} failed, "
        f"{counts['errors']} errors, {counts['skipped']} skipped or xfailed",
        "",
    ]
    if skipped:
        lines += [
            "**Not measured in this run** — each is absent from the green, not part of it:",
            "",
            "| | test | reason |",
            "|---|---|---|",
        ]
        lines += [
            f"| {kind} | `{_cell(name)}` | {_cell(reason)} |" for kind, name, reason in skipped
        ]
    else:
        lines.append("Nothing was skipped.")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    print(summarize(Path(argv[0])))
    for report in argv[1:]:
        path = Path(report)
        if path.is_file():
            print(path.read_text())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
