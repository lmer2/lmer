"""Shared runtime-capability checks for lmer integration tests.

These helpers decide whether a test that shells out to the real `lmer`
script (which in turn runs a container) is able to run in the current
environment. Tests that require a live container runtime or a working
lmer venv should gate themselves with the `requires_container` /
`requires_lmer_venv` marks exported here.
"""
import shutil
import subprocess
from pathlib import Path

import pytest


def has_container_runtime() -> bool:
    """Check if a working Docker or Podman daemon is reachable.

    Verifies both that the binary exists and that its daemon responds,
    so tests that exec real container commands skip cleanly when the
    socket isn't mounted (e.g. nested containers without docker-in-docker).
    """
    for runtime in ("docker", "podman"):
        if shutil.which(runtime) is None:
            continue
        try:
            result = subprocess.run(
                [runtime, "info"],
                capture_output=True,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode == 0:
            return True
    return False


def has_lmer_venv() -> bool:
    """Check if the repo's `lmer` script has a working virtual environment."""
    lmer_path = Path(__file__).parent.parent / "lmer"
    if not lmer_path.exists():
        return False
    result = subprocess.run(
        [str(lmer_path), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return "No virtual environment found" not in (result.stdout + result.stderr)


requires_container = pytest.mark.skipif(
    not has_container_runtime(),
    reason="Requires a running Docker or Podman daemon",
)

requires_lmer_venv = pytest.mark.skipif(
    not has_lmer_venv(),
    reason="lmer requires a working virtual environment",
)
