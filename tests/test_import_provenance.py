"""The suite must be testing THIS checkout — issue #198.

A self-development container carries ``PYTHONPATH=/Agents/global/src:…``, and
``PYTHONPATH`` precedes site-packages, so it beats the venv's editable ``.pth``
however that ``.pth`` is pointed. The result is a test run that imports lmer
from the *operational runtime* while the developer edits ``/workspace/src``.
When the edit adds a new symbol that fails loudly; when it changes an existing
one it does not fail at all, and the suite reports green about code it never
executed.

These tests are the assertion that closes that hole. They are deliberately:

* **unconditional** — not gated on ``LMER_SELF_DEV``. "This suite tests this
  checkout" is a universal invariant, and a guard that only arms itself inside
  one kind of container is a guard nobody ever watches fail.
* **about resolution, not about the mechanism.** They say nothing about which
  file set ``PYTHONPATH``, or whether a ``.pth`` exists. Any future export,
  wrapper or launcher that reintroduces the shadowing fails here — including
  ones that do not exist yet. That is the property #198 asks for: the previous
  fix was defeated by an export in a *different file* than the one whose
  comment claimed the problem was handled.

Failure messages name both the resolved path and the repo root, because that
is exactly the comparison the reader is trying to make.
"""
import importlib
import importlib.util
from pathlib import Path

import pytest

import lmer_cli

#: The checkout these tests live in. Everything the suite imports from this
#: project must come from under here.
REPO_ROOT = Path(__file__).resolve().parent.parent

SRC_DIR = REPO_ROOT / "src"


def _top_level_packages():
    """Top-level importable packages shipped in this repo's ``src/``.

    Directory listing rather than a hardcoded list: a package added to
    ``src/`` is covered the day it lands, with nothing to remember to update.
    """
    if not SRC_DIR.is_dir():
        return []
    return sorted(
        entry.name
        for entry in SRC_DIR.iterdir()
        if entry.is_dir() and (entry / "__init__.py").is_file()
    )


def _provenance_failure(name: str, resolved: Path) -> str:
    return (
        f"{name} resolved to:\n"
        f"    {resolved}\n"
        f"but this test suite lives in:\n"
        f"    {REPO_ROOT}\n"
        "\n"
        f"The suite is exercising a DIFFERENT copy of lmer than the checkout "
        f"under test, so its result says nothing about the working tree "
        f"(issue #198).\n"
        "\n"
        "In a self-development container this means PYTHONPATH still puts the "
        "operational tree (/Agents/global/src) ahead of /workspace/src. Check "
        "the self-dev block in libexec/claude-runner.sh and "
        "libexec/harness-common.sh, and the PYTHONPATH exported by "
        "Ctl/container/entrypoint.sh.\n"
        "\n"
        f"Workaround for the current shell: PYTHONPATH={SRC_DIR} <command>"
    )


def test_imported_lmer_cli_comes_from_this_checkout():
    """The fact: where the interpreter running this suite actually got lmer_cli.

    ``__file__`` on the imported module is reported by the import system
    itself, after the fact — not a prediction about how a future import would
    resolve. If this passes, the code under test is the code in this tree.
    """
    resolved = Path(lmer_cli.__file__).resolve()
    assert resolved.is_relative_to(REPO_ROOT), _provenance_failure(
        "lmer_cli (as imported by this test run)", resolved
    )


@pytest.mark.parametrize("package", _top_level_packages())
def test_src_package_resolves_inside_this_checkout(package):
    """Every package in ``src/`` resolves here, not to another copy.

    ``find_spec`` locates the module without executing it, so a package whose
    optional third-party dependencies are absent is still *checked* rather
    than quietly skipped — a skip here would be indistinguishable from the
    failure this test exists to catch.
    """
    spec = importlib.util.find_spec(package)
    assert spec is not None and spec.origin, (
        f"{package} is a package in {SRC_DIR} but Python cannot locate it at "
        f"all. The suite cannot confirm what it would import."
    )

    resolved = Path(spec.origin).resolve()
    assert resolved.is_relative_to(REPO_ROOT), _provenance_failure(
        package, resolved
    )


@pytest.mark.parametrize(
    "script", ["libexec/claude-runner.sh", "libexec/harness-common.sh"]
)
def test_runners_pin_the_supervisor_to_the_operational_tree(script):
    """The supervisor is the one process the self-dev view must NOT reach.

    The inverse invariant of everything above: the agent's tooling imports the
    /workspace checkout (#198), but the supervisor is operational
    infrastructure — the platform's only way to reach the session — and a
    supervisor imported from the agent's checkout serves whatever routes THAT
    code has (#236: a checkout of `main` cost every self-dev session its
    /resize route and the #210 submit fix). Both runner scripts must launch it
    with the operational tree pinned ahead of the self-dev view, via a
    sys.path insert rather than an env override, so the pin does not leak into
    the harness child and undo #198.

    A source-level check, like the container-passthrough guards in
    tests/test_gates.py: the property lives in a shell script the suite cannot
    execute, and what this catches is the pin being dropped or reworded away.
    """
    text = (REPO_ROOT / script).read_text(encoding="utf-8")
    assert "sys.path.insert(0, \"/Agents/global/src\")" in text, (
        f"{script} no longer pins lmer-supervisor's imports to "
        "/Agents/global/src. In a self-dev session the supervisor would "
        "import the agent's /workspace checkout and serve whatever control "
        "plane THAT code has (#236)."
    )
    assert "from lmer_cli.supervisor import main" in text, (
        f"{script} pins sys.path but no longer launches the supervisor "
        "through the pinned interpreter — the pin is decoration unless the "
        "supervisor is imported under it (#236)."
    )
