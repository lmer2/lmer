"""The shell-side readers of ``LMER_CONTAINER_RUNTIME``: the Makefile and
``bin/lmer-reset`` (``scripts/platform-container-run.sh`` is covered in
``test_platform_container_run.py``).

Each must accept exactly what ``lmer_cli.runtime.detect_runtime()`` accepts —
trimmed, case-insensitive, ``docker`` or ``podman`` — and reject anything else
before running it. Runtimes are stubs on a scratch ``PATH``; nothing here
touches a real container engine.
"""

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = REPO_ROOT / "Makefile"
LMER_RESET = REPO_ROOT / "bin" / "lmer-reset"


def _stub(bin_dir, name, body="exit 1"):
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


# --- Makefile -------------------------------------------------------------------

MAKE = shutil.which("make")


def _make(bin_dir, *targets, value, dry_run=True):
    """``make -n`` with only *bin_dir* on PATH, so a host runtime cannot answer."""
    return subprocess.run(
        [MAKE, *(["-n"] if dry_run else []), "-f", str(MAKEFILE), *targets],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        env={"PATH": str(bin_dir), "LMER_CONTAINER_RUNTIME": value},
    )


@pytest.mark.skipif(MAKE is None, reason="make is not installed")
class TestMakefile:

    @pytest.fixture
    def bin_dir(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        return bin_dir

    def test_a_mixed_case_padded_override_picks_that_runtime(self, bin_dir):
        _stub(bin_dir, "docker")
        podman = _stub(bin_dir, "podman")
        result = _make(bin_dir, "info", value=" PodMan ")
        assert result.returncode == 0, result.stderr
        assert f"Container Runtime: {podman}" in result.stdout

    @pytest.mark.parametrize("value", ["nerdctl", "/usr/bin/podman", "docker podman"])
    def test_an_unsupported_value_fails_the_recipe(self, bin_dir, value):
        _stub(bin_dir, "podman")
        result = _make(bin_dir, "build", value=value)
        assert result.returncode != 0
        assert "is not supported; use docker or podman" in result.stderr

    def test_a_missing_runtime_fails_the_recipe(self, bin_dir):
        _stub(bin_dir, "docker")
        result = _make(bin_dir, "build", value="podman")
        assert result.returncode != 0
        assert "podman is not on PATH" in result.stderr

    def test_security_scans_container_fallback_reports_a_bad_value(self, bin_dir):
        """Run for real: with no trivy on the stub-only PATH the recipe takes
        the containerized fallback, which must stop before running anything."""
        result = _make(bin_dir, "security-scan", value="nerdctl", dry_run=False)
        assert result.returncode != 0
        assert "is not supported; use docker or podman" in result.stderr
        assert "command not found" not in result.stderr

    @pytest.mark.parametrize("target", ["help", "version", "test"])
    def test_a_bad_value_does_not_break_targets_without_a_runtime(self, bin_dir, target):
        result = _make(bin_dir, target, value="nonexistent-rt")
        assert result.returncode == 0, result.stderr


# --- bin/lmer-reset ---------------------------------------------------------------

class TestLmerReset:

    @pytest.fixture
    def host(self, tmp_path):
        """A HOME with no ~/.lmer and a stub ``uv`` that lists nothing, so the
        script finds nothing to remove; stdin is closed, so a prompt aborts."""
        home = tmp_path / "home"
        home.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _stub(bin_dir, "uv")
        return home, bin_dir

    def _run(self, host, value):
        home, bin_dir = host
        return subprocess.run(
            ["bash", str(LMER_RESET)],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
            env={
                "HOME": str(home),
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "LMER_CONTAINER_RUNTIME": value,
            },
        )

    def test_an_unsupported_value_is_rejected_before_it_runs(self, host, tmp_path):
        _, bin_dir = host
        marker = tmp_path / "ran"
        _stub(bin_dir, "evil", f"touch {marker}")
        result = self._run(host, "evil")
        assert result.returncode == 1
        assert "is not supported; use docker or podman" in result.stderr
        assert not marker.exists()

    def test_a_mixed_case_padded_override_drives_that_runtime(self, host, tmp_path):
        _, bin_dir = host
        calls = tmp_path / "calls"
        _stub(bin_dir, "podman", f'echo "$*" >> {calls}; exit 1')
        result = self._run(host, " PodMan ")
        assert result.returncode == 0, result.stderr
        assert "image inspect lmer" in calls.read_text()
