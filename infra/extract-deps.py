"""Emit a pip requirements list from pyproject.toml.

The Dockerfiles install dependencies *without* installing the project itself, so
the dependency layer stays cached across source changes and there is exactly one
place where dependencies are declared.

    python infra/extract-deps.py            -> base dependencies
    python infra/extract-deps.py worker dev -> base + those optional groups
"""

import pathlib
import sys
import tomllib

project = tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]
requirements = list(project["dependencies"])
for group in sys.argv[1:]:
    requirements += project["optional-dependencies"][group]

print("\n".join(requirements))
