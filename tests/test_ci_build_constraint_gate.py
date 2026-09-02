"""Behavioural tests for ci/check_build_constraint.py.

The gate's whole job is to FAIL: it exists so that a build backend which
escaped `[tool.uv] build-constraint-dependencies` is caught before the tag
pipeline reaches publish, where the version is already spent (ctl #44). The
passing side is exercised by every real build; these tests exercise the
failing side, against synthetic wheels rather than a real `uv build`.
"""

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "ci" / "check_build_constraint.py"

PYPROJECT = """\
[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"

[project]
name = "probe"
version = "0.0.0"

[tool.uv]
build-constraint-dependencies = [{constraints}]
"""


def write_wheel(dist: Path, generator: str | None, name: str = "probe-0.0.0"):
    """A wheel carrying just the .dist-info/WHEEL the gate reads."""
    dist.mkdir(parents=True, exist_ok=True)
    path = dist / f"{name}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        body = "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        if generator is not None:
            body = f"Wheel-Version: 1.0\nGenerator: {generator}\nTag: py3-none-any\n"
        archive.writestr(f"{name}.dist-info/WHEEL", body)
    return path


@pytest.fixture
def repo(tmp_path):
    """A throwaway repo root with the gate copied in at ci/."""
    (tmp_path / "ci").mkdir()
    (tmp_path / "ci" / GATE.name).write_bytes(GATE.read_bytes())

    def build(constraints='"setuptools==84.0.0"'):
        (tmp_path / "pyproject.toml").write_text(
            PYPROJECT.format(constraints=constraints)
        )
        return tmp_path

    return build


def run(root):
    return subprocess.run(
        [sys.executable, str(root / "ci" / GATE.name)],
        capture_output=True,
        text=True,
        cwd=root,
    )


class TestPasses:
    def test_wheel_built_by_the_constrained_setuptools(self, repo):
        root = repo()
        write_wheel(root / "dist", "setuptools (84.0.0)")
        result = run(root)
        assert result.returncode == 0, result.stderr
        assert "built by the constrained setuptools 84.0.0" in result.stdout

    def test_every_wheel_is_checked_not_just_the_first(self, repo):
        root = repo()
        write_wheel(root / "dist", "setuptools (84.0.0)", name="probe-0.0.0")
        write_wheel(root / "dist", "setuptools (85.0.0)", name="probe-0.0.1")
        result = run(root)
        assert result.returncode == 1
        assert "85.0.0" in result.stderr


class TestFailsClosed:
    def test_wheel_built_by_a_different_setuptools(self, repo):
        """The drift the gate exists for: the constraint did not bind."""
        root = repo()
        write_wheel(root / "dist", "setuptools (85.0.0)")
        result = run(root)
        assert result.returncode == 1
        assert "was built by setuptools 85.0.0" in result.stderr
        assert "constrains the build to 84.0.0" in result.stderr
        assert "did not bind" in result.stderr

    def test_wheel_built_by_a_different_backend(self, repo):
        root = repo()
        write_wheel(root / "dist", "hatchling 1.27.0")
        result = run(root)
        assert result.returncode == 1
        assert "no `Generator: setuptools (<version>)` line" in result.stderr

    def test_wheel_with_no_generator_line(self, repo):
        root = repo()
        write_wheel(root / "dist", None)
        result = run(root)
        assert result.returncode == 1
        assert "no `Generator: setuptools (<version>)` line" in result.stderr

    def test_no_constraint_declared(self, repo):
        """An unpinned backend must fail loudly, not pass vacuously."""
        root = repo(constraints="")
        write_wheel(root / "dist", "setuptools (84.0.0)")
        result = run(root)
        assert result.returncode == 1
        assert "does not pin setuptools with `==`" in result.stderr

    def test_constraint_for_a_different_package_does_not_count(self, repo):
        root = repo(constraints='"wheel==0.45.0"')
        write_wheel(root / "dist", "setuptools (84.0.0)")
        result = run(root)
        assert result.returncode == 1
        assert "does not pin setuptools with `==`" in result.stderr

    def test_empty_dist(self, repo):
        root = repo()
        (root / "dist").mkdir()
        result = run(root)
        assert result.returncode == 1
        assert "no wheel in dist/" in result.stderr

    def test_no_dist_at_all(self, repo):
        result = run(repo())
        assert result.returncode == 1
        assert "no wheel in dist/" in result.stderr
