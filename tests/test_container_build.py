"""Test that the container image actually builds successfully.

This is an integration test that runs `make build` and verifies
the image is created. It catches real build failures that static
Containerfile linting cannot detect.
"""

import subprocess
import pytest

from tests._lmer_runtime import requires_container


pytestmark = requires_container

# Use a distinct tag so we don't clobber any working image
BUILD_TAG = "lmer:test-build"


@pytest.fixture(scope="module")
def container_runtime():
    """Detect available container runtime (docker or podman)."""
    for cmd in ("docker", "podman"):
        if subprocess.run(
            ["which", cmd], capture_output=True
        ).returncode == 0:
            return cmd
    pytest.skip("No container runtime (docker/podman) available")


@pytest.fixture(scope="module")
def project_root_mod():
    """Module-scoped project root (avoids scope mismatch with conftest)."""
    from pathlib import Path
    return Path(__file__).parent.parent


@pytest.fixture(scope="module")
def build_result(project_root_mod, container_runtime):
    """Run `make build` once for the entire module and return the result."""
    result = subprocess.run(
        ["make", "build", "FULL_IMAGE=lmer:test-build"],
        cwd=str(project_root_mod),
        capture_output=True,
        text=True,
        timeout=600,
    )
    yield result
    # Cleanup: remove the test image
    subprocess.run(
        [container_runtime, "rmi", BUILD_TAG],
        capture_output=True,
    )


@pytest.mark.slow
class TestContainerBuild:
    """Test that the container image builds end-to-end."""

    def test_build_succeeds(self, build_result):
        """The container must build without errors."""
        assert build_result.returncode == 0, (
            f"Container build failed (exit {build_result.returncode}).\n"
            f"--- stdout ---\n{build_result.stdout[-3000:]}\n"
            f"--- stderr ---\n{build_result.stderr[-3000:]}"
        )

    def test_image_exists_after_build(self, build_result, container_runtime):
        """After a successful build the image should be present."""
        if build_result.returncode != 0:
            pytest.skip("Build failed; nothing to inspect")
        result = subprocess.run(
            [container_runtime, "image", "inspect", BUILD_TAG],
            capture_output=True,
        )
        assert result.returncode == 0, f"Image {BUILD_TAG} not found after build"

    def test_python3_available(self, build_result, container_runtime):
        """Python 3.12+ must be usable inside the image."""
        if build_result.returncode != 0:
            pytest.skip("Build failed")
        result = subprocess.run(
            [container_runtime, "run", "--rm", BUILD_TAG,
             "python3", "-c",
             "import sys; assert sys.version_info >= (3, 12), sys.version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Python version check failed:\n{result.stderr}"
        )

    def test_uv_available(self, build_result, container_runtime):
        """uv package manager must be on PATH."""
        if build_result.returncode != 0:
            pytest.skip("Build failed")
        result = subprocess.run(
            [container_runtime, "run", "--rm", BUILD_TAG,
             "bash", "-c", "uv --version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"uv not available:\n{result.stderr}"
        )

    def test_mise_available(self, build_result, container_runtime):
        """mise tool manager must be on PATH."""
        if build_result.returncode != 0:
            pytest.skip("Build failed")
        result = subprocess.run(
            [container_runtime, "run", "--rm", BUILD_TAG,
             "bash", "-c", "mise --version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"mise not available:\n{result.stderr}"
        )

    def test_venv_exists(self, build_result, container_runtime):
        """The project venv at /Agents/global/.venv must exist."""
        if build_result.returncode != 0:
            pytest.skip("Build failed")
        result = subprocess.run(
            [container_runtime, "run", "--rm", BUILD_TAG,
             "test", "-f", "/Agents/global/.venv/bin/python"],
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, "venv not found at /Agents/global/.venv"

    def test_doctor_runs_clean(self, build_result, container_runtime):
        """bin/doctor must run to completion inside the container.

        A bare `docker run` without bind mounts, credentials, or full
        shell init will legitimately report some errors (missing
        .credentials.json, node needing mise activation, etc.).  We
        verify the script itself doesn't crash (exit 2) and produces
        a complete report covering every check category.
        """
        if build_result.returncode != 0:
            pytest.skip("Build failed")
        result = subprocess.run(
            [container_runtime, "run", "--rm", BUILD_TAG,
             "bash", "-c", "/Agents/global/bin/doctor -v"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Exit 0 = healthy, 1 = errors found, 2 = doctor itself broke
        assert result.returncode in (0, 1), (
            f"doctor crashed (exit {result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )
        # Verify every check category ran
        for section in [
            "Slash Commands",
            "Settings & Credentials",
            "Global Rules",
            "Required Tools",
            "Gate Commands",
            "Environment",
            "Workspace",
            "SSH Agent",
            "Python Environment",
            "Summary",
        ]:
            assert section in result.stdout, (
                f"doctor output missing '{section}' section:\n"
                f"{result.stdout}"
            )

    def test_runs_as_developer_user(self, build_result, container_runtime):
        """Container must run as 'developer', not root."""
        if build_result.returncode != 0:
            pytest.skip("Build failed")
        result = subprocess.run(
            [container_runtime, "run", "--rm", "--entrypoint", "whoami",
             BUILD_TAG],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "developer", (
            f"Expected 'developer', got '{result.stdout.strip()}'"
        )
