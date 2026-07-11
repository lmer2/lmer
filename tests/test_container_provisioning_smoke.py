#!/usr/bin/env python3
"""Container smoke tests for the provisioning fixes shipped in this bundle.

Unlike the mocked-path unit tests, these exercise the two fixes in the REAL
container filesystem by shelling out to the `lmer` binary with
``--no-task --exec '<cmd>'`` (mirroring ``tests/test_lmer_integration.py``):

- Fix 1: ``/napkin`` (+ ``/taskdef``) are pre-created in the image so
  ``ensure_clone`` can populate them — ``~/napkin`` resolves to a real
  checkout instead of a dangling symlink.
- Fix 2: the gate test-interpreter selection resolves to a PATH python that
  can import pytest inside the container.

The whole module is gated with ``requires_container`` /
``requires_lmer_venv`` so it skips cleanly without a runtime socket. Test B
additionally skips when no napkin repo URL is present in the environment
rather than hard-failing on missing credentials.
"""
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests._lmer_runtime import requires_container, requires_lmer_venv

LMER = Path(__file__).parent.parent / "lmer"


def _run_lmer_exec(inner_cmd: str, extra_env: dict | None = None):
    """Run ``lmer --no-task --exec '<inner_cmd>'`` from a temp cwd."""
    env = {**os.environ, **(extra_env or {})}
    with tempfile.TemporaryDirectory() as tmpdir:
        return subprocess.run(
            ["bash", "-c",
             f"cd {tmpdir} && {LMER} --no-task --exec {inner_cmd!r}"],
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )


@requires_container
@requires_lmer_venv
class TestContainerProvisioningSmoke:
    """Smoke-test the two provisioning fixes in a real container."""

    def test_gate_interpreter_can_import_pytest(self):
        """Fix 2: a PATH python that can import pytest exists in-container."""
        result = _run_lmer_exec(
            'python -c "import pytest" && echo PYTEST_OK'
        )
        assert result.returncode == 0, (
            "in-container python could not import pytest. "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "PYTEST_OK" in result.stdout, (
            f"expected PYTEST_OK in stdout, got: {result.stdout!r}"
        )

    def test_napkin_repo_provisioned(self):
        """Fix 1: ~/napkin resolves to a real checkout when a URL is given."""
        napkin_repo = os.environ.get("LMER_NAPKIN_REPO")
        if not napkin_repo:
            pytest.skip(
                "no napkin repo URL in environment (set LMER_NAPKIN_REPO)"
            )

        result = _run_lmer_exec(
            "test -e ~/napkin/.git && echo NAPKIN_OK",
            extra_env={"LMER_NAPKIN_REPO": napkin_repo},
        )
        assert result.returncode == 0, (
            "~/napkin/.git not present in container (dangling clone?). "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "NAPKIN_OK" in result.stdout, (
            f"expected NAPKIN_OK in stdout, got: {result.stdout!r}"
        )
