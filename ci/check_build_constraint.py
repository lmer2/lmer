#!/usr/bin/env python3
"""Check that the build backend constraint actually bound the build.

`build-system.requires` names setuptools with a floor only, and build
requirements resolve outside `uv.lock`, so the only thing holding the backend
still is the `[tool.uv] build-constraint-dependencies` pin in pyproject.toml. A
constraint that silently failed to apply would look exactly like no constraint
at all, and the drift that killed ctl's second v2.0.0 tag (ctl #44) would be
live here too: a newer setuptools emits a newer Metadata-Version, and the
publish action's twine rejects the wheel after trusted publishing has already
authenticated.

So this does not trust the constraint — it asks the wheel who built it. Run it
after `uv build`, from the repository root. Stdlib only.
"""

import re
import sys
import tomllib
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# setuptools writes its own line into .dist-info/WHEEL:
#     Generator: setuptools (84.0.0)
GENERATOR = re.compile(r"^Generator:\s*setuptools\s*\((?P<version>[^)]+)\)\s*$", re.M)


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def constrained_setuptools() -> str:
    """The setuptools version pyproject.toml constrains the build to."""
    with (REPO / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)
    constraints = pyproject.get("tool", {}).get("uv", {}).get(
        "build-constraint-dependencies", []
    )
    pins = [c for c in constraints if c.replace(" ", "").startswith("setuptools==")]
    if not pins:
        fail(
            "pyproject.toml [tool.uv] build-constraint-dependencies does not pin "
            "setuptools with `==`. build-system.requires carries a floor only, so "
            "without that pin the backend — and the metadata version we publish — "
            "floats."
        )
    return pins[0].replace(" ", "").split("==", 1)[1]


def built_with(wheel: Path) -> str:
    """The setuptools version that actually produced the wheel, per the wheel."""
    with zipfile.ZipFile(wheel) as archive:
        names = [n for n in archive.namelist() if n.endswith(".dist-info/WHEEL")]
        if len(names) != 1:
            fail(f"{wheel.name} does not contain exactly one .dist-info/WHEEL")
        found = GENERATOR.search(archive.read(names[0]).decode())
    if not found:
        fail(f"{wheel.name}'s WHEEL has no `Generator: setuptools (<version>)` line")
    return found.group("version")


def main() -> int:
    wheels = sorted((REPO / "dist").glob("*.whl"))
    if not wheels:
        fail("no wheel in dist/ — run `uv build` first")

    want = constrained_setuptools()
    for wheel in wheels:
        got = built_with(wheel)
        if got != want:
            fail(
                f"{wheel.name} was built by setuptools {got}, but pyproject.toml "
                f"constrains the build to {want}. The build constraint did not "
                "bind — the backend, and the metadata version, are floating."
            )
        print(f"{wheel.name}: built by the constrained setuptools {got}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
