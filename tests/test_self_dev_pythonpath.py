"""Self-dev runners put /workspace/src ahead of the operational tree — #198.

**Why this exists alongside tests/test_import_provenance.py.** The provenance
test is the real assertion, but it cannot cover this particular regression.
``gate-check``'s ``check_tests`` prepends ``<project>/src`` to the pytest
subprocess's ``PYTHONPATH`` on its own, so if someone deleted the export these
tests guard, provenance would still pass *under the gate* — the breakage would
only surface later, in a bare ``pytest`` or ``python -c`` in some future
session's shell. That is precisely the shape of the original bug, and it is why
the export needs a guard of its own.

**What these are, honestly.** They read the runner scripts as text. They are
regression protection for two specific lines, not a guarantee about a running
container: nothing here proves what a real session's ``PYTHONPATH`` ends up
being. ``test_import_provenance.py`` is what proves that, at the point it
matters.

Executing the self-dev block instead was considered and rejected: both copies
write to ``/Agents/global/.venv/.../__editable__.lmer*.pth`` at hardcoded
absolute paths, so running them would modify the **operational runtime** — the
one thing self-dev mode forbids. Parameterizing those paths would mean a new
env seam, its container passthrough, its doc bullet and its own guard test, to
reach a weaker assertion than the provenance test already gives.
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIBEXEC = REPO_ROOT / "libexec"

#: Both places that detect self-development mode. harness-common.sh carries the
#: shared implementation for newer runners; claude-runner.sh predates it and
#: keeps a behavior-identical inline copy (see the header of harness-common.sh).
#: A fix that lands in one and not the other leaves half the harnesses broken,
#: so every check below runs against both.
SELF_DEV_SCRIPTS = ["claude-runner.sh", "harness-common.sh"]

#: A line assigning PYTHONPATH, capturing the assigned value.
PYTHONPATH_ASSIGNMENT = re.compile(r'^\s*(?:export\s+)?PYTHONPATH=(.+)$', re.MULTILINE)


def _script(name: str) -> str:
    return (LIBEXEC / name).read_text()


def _pythonpath_assignments(source: str):
    return [m.group(1).strip() for m in PYTHONPATH_ASSIGNMENT.finditer(source)]


@pytest.mark.parametrize("script", SELF_DEV_SCRIPTS)
def test_self_dev_prepends_workspace_src_to_pythonpath(script):
    """The dev checkout goes in FRONT of whatever PYTHONPATH already held.

    The container entrypoint exports ``PYTHONPATH=/Agents/global/src:…`` before
    either runner gets to decide anything, and PYTHONPATH precedes
    site-packages — so without this prepend the operational tree wins no matter
    where the editable ``.pth`` points.
    """
    assignments = _pythonpath_assignments(_script(script))
    assert assignments, (
        f"libexec/{script} no longer assigns PYTHONPATH at all. Self-dev "
        f"sessions will import lmer from the operational tree (#198)."
    )

    prepending = [a for a in assignments if a.lstrip('"').startswith("/workspace/src")]
    assert prepending, (
        f"libexec/{script} assigns PYTHONPATH but never puts /workspace/src "
        f"first. Found: {assignments}. Self-dev sessions will import lmer from "
        f"/Agents/global/src instead of the checkout (#198)."
    )


@pytest.mark.parametrize("script", SELF_DEV_SCRIPTS)
def test_self_dev_keeps_operational_tree_as_fallback(script):
    """/Agents/global/src stays behind it, deliberately.

    Top-level packages that exist only on the operational ref (the reason the
    editable ``.pth`` lists two directories) must still resolve rather than
    vanish from sys.path.
    """
    prepending = [
        a
        for a in _pythonpath_assignments(_script(script))
        if a.lstrip('"').startswith("/workspace/src")
    ]
    assert any("$PYTHONPATH" in a or "${PYTHONPATH" in a for a in prepending), (
        f"libexec/{script} overwrites PYTHONPATH instead of prepending to it, "
        f"dropping the /Agents/global/src fallback. Found: {prepending}"
    )


@pytest.mark.parametrize("script", SELF_DEV_SCRIPTS)
def test_self_dev_banner_states_the_resolved_path(script):
    """The session says which tree it imports, at startup, unconditionally.

    A developer should be able to check this as a fact rather than discover it
    from an AttributeError several commands later — which is how #198 was
    found.
    """
    source = _script(script)
    assert "import lmer_cli; print(lmer_cli.__file__)" in source, (
        f"libexec/{script} no longer resolves lmer_cli's path for the self-dev "
        f"banner, so a session cannot report which tree it imports (#198)."
    )
    assert "lmer_cli resolves to" in source, (
        f"libexec/{script} resolves lmer_cli's path but no longer prints it. "
        f"The banner line is the visible half of the #198 fix."
    )


@pytest.mark.parametrize("script", SELF_DEV_SCRIPTS)
def test_self_dev_warns_when_resolution_is_wrong(script):
    """A path outside /workspace is called out, not printed as if it were fine."""
    source = _script(script)
    assert "NOT the /workspace checkout" in source, (
        f"libexec/{script} prints the resolved lmer_cli path but no longer "
        f"distinguishes a wrong one. The banner must flag a session that would "
        f"exercise the operational runtime (#198)."
    )
