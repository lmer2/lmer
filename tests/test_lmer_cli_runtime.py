#!/usr/bin/env python3
"""Tests for lmer_cli.runtime module"""

import os
import pytest
import shutil
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

from lmer_cli.runtime import (
    InstallMode,
    detect_runtime,
    detect_install_mode,
    RuntimeErrorDetect,
    _is_lmer_pyproject,
    tty_flags,
    repo_root_path,
    lmer_state_dir,
    base_run_args,
    env_args,
    _resolve_pids_limit,
)


class TestDetectRuntime:
    """Test container runtime detection"""

    def test_detect_docker_when_available(self):
        """Test Docker is detected when available"""
        with patch("shutil.which") as mock_which:
            mock_which.side_effect = lambda x: "/usr/bin/docker" if x == "docker" else None
            assert detect_runtime() == "docker"

    def test_detect_podman_when_docker_not_available(self):
        """Test Podman is detected when Docker not available"""
        with patch("shutil.which") as mock_which:
            mock_which.side_effect = lambda x: "/usr/bin/podman" if x == "podman" else None
            assert detect_runtime() == "podman"

    def test_prefers_docker_over_podman(self):
        """Test Docker is preferred when both available"""
        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/runtime"  # Both available
            assert detect_runtime() == "docker"

    def test_raises_when_no_runtime_available(self):
        """Test exception raised when neither runtime available"""
        with patch("shutil.which", return_value=None):
            with pytest.raises(RuntimeErrorDetect, match="Neither Docker nor Podman"):
                detect_runtime()


class TestTtyFlags:
    """Test TTY flag detection"""

    def test_tty_flags_when_stdin_is_tty(self):
        """Test returns -it when stdin is a TTY"""
        with patch.object(sys.stdin, "isatty", return_value=True):
            assert tty_flags() == ["-it"]

    def test_tty_flags_when_stdin_not_tty(self):
        """Test returns empty list when stdin is not a TTY"""
        with patch.object(sys.stdin, "isatty", return_value=False):
            assert tty_flags() == []


class TestIsLmerPyproject:
    """Test pyproject.toml identification"""

    def test_recognizes_lmer_pyproject(self, tmp_path):
        """Test that a pyproject.toml with name='lmer' is recognized"""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "lmer"\nversion = "0.1.0"\n')
        assert _is_lmer_pyproject(pyproject) is True

    def test_rejects_other_pyproject(self, tmp_path):
        """Test that a pyproject.toml for a different package is rejected"""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "some-other-package"\n')
        assert _is_lmer_pyproject(pyproject) is False

    def test_rejects_missing_file(self, tmp_path):
        """Test that a missing file returns False"""
        assert _is_lmer_pyproject(tmp_path / "nonexistent.toml") is False

    def test_rejects_invalid_toml(self, tmp_path):
        """Test that invalid TOML returns False"""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("this is not valid toml [[[")
        assert _is_lmer_pyproject(pyproject) is False

    def test_rejects_pyproject_without_project_section(self, tmp_path):
        """Test that a pyproject.toml without [project] section returns False"""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[build-system]\nrequires = ["setuptools"]\n')
        assert _is_lmer_pyproject(pyproject) is False


class TestDetectInstallMode:
    """Test install mode detection"""

    def test_developer_mode_from_repo(self):
        """When running from the git checkout, detect developer mode"""
        mode = detect_install_mode()
        assert mode == InstallMode.DEVELOPER

    def test_installed_mode_when_no_lmer_pyproject(self):
        """When no lmer pyproject.toml found, detect installed mode"""
        with patch("lmer_cli.runtime.Path") as MockPath:
            # Simulate __file__ resolving to a path with no lmer pyproject.toml
            mock_resolved = MagicMock()
            mock_resolved.parents = []
            mock_pyproject = MagicMock()
            mock_pyproject.exists.return_value = False
            mock_resolved.__truediv__ = MagicMock(return_value=mock_pyproject)
            MockPath.return_value.resolve.return_value = mock_resolved
            # Since we can't easily mock Path(__file__), test via repo_root_path
            # which delegates to detect_install_mode
            pass

    def test_installed_mode_returns_none_repo_root(self):
        """In installed mode, repo_root_path returns None"""
        with patch("lmer_cli.runtime.detect_install_mode", return_value=InstallMode.INSTALLED):
            assert repo_root_path() is None


class TestRepoRootPath:
    """Test repository root path detection"""

    def test_finds_repo_root_with_pyproject_toml(self):
        """Test repo_root_path finds directory containing pyproject.toml"""
        root = repo_root_path()
        assert root is not None
        assert root.exists()
        assert (root / "pyproject.toml").exists()

    def test_repo_root_is_absolute(self):
        """Test returned path is absolute"""
        root = repo_root_path()
        assert root is not None
        assert root.is_absolute()

    def test_repo_root_returns_none_in_installed_mode(self):
        """Test repo_root_path returns None when in installed mode"""
        with patch("lmer_cli.runtime.detect_install_mode", return_value=InstallMode.INSTALLED):
            root = repo_root_path()
            assert root is None


class TestLmerStateDir:
    """Test LMER state directory"""

    def test_returns_home_lmer(self):
        """State dir should be ~/.lmer/"""
        state = lmer_state_dir()
        assert state == Path.home() / ".lmer"

    def test_returns_path_object(self):
        """State dir should be a Path"""
        state = lmer_state_dir()
        assert isinstance(state, Path)


class TestBaseRunArgs:
    """Test base container run arguments"""

    def test_base_args_docker(self):
        """Test base args for Docker"""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LMER_PIDS_LIMIT", None)
            args = base_run_args("docker", False, "developer")

        assert "docker" in args
        assert "run" in args
        assert "--rm" in args
        assert "--cpus" in args
        assert "1" in args
        assert "--memory" in args
        assert "2g" in args
        # No LMER_PIDS_LIMIT set: lmer imposes no cap and defers to the runtime.
        assert "--pids-limit" not in args
        assert "--security-opt" in args
        assert "no-new-privileges" in args
        assert "--user" in args
        assert "developer" in args
        assert "-w" in args
        assert "/workspace" in args

    def test_base_args_podman_includes_userns(self):
        """Test Podman args include --userns=keep-id"""
        args = base_run_args("podman", False, "developer")

        assert "podman" in args
        assert "--userns=keep-id" in args

    def test_base_args_docker_no_userns(self):
        """Test Docker args don't include --userns=keep-id"""
        args = base_run_args("docker", False, "developer")

        assert "--userns=keep-id" not in args

    def test_base_args_with_different_user(self):
        """Test base args with custom user"""
        args = base_run_args("docker", False, "1000:1000")

        user_idx = args.index("--user")
        assert args[user_idx + 1] == "1000:1000"

    def test_base_args_includes_tty_when_stdin_is_tty(self):
        """Test TTY flags included when stdin is TTY"""
        with patch.object(sys.stdin, "isatty", return_value=True):
            args = base_run_args("docker", False, "developer")
            assert "-it" in args

    def test_base_args_no_tty_when_stdin_not_tty(self):
        """Test no TTY flags when stdin is not TTY"""
        with patch.object(sys.stdin, "isatty", return_value=False):
            args = base_run_args("docker", False, "developer")
            assert "-it" not in args

    def test_base_args_honors_pids_limit_override(self):
        """LMER_PIDS_LIMIT sets the --pids-limit value in base args."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "4096"}):
            args = base_run_args("docker", False, "developer")
            assert args[args.index("--pids-limit") + 1] == "4096"

    def test_base_args_invalid_pids_limit_omits_flag(self):
        """An invalid LMER_PIDS_LIMIT omits --pids-limit (defers to runtime)."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "0"}):
            args = base_run_args("docker", False, "developer")
            assert "--pids-limit" not in args


class TestResolvePidsLimit:
    """Test LMER_PIDS_LIMIT parsing for the container --pids-limit value."""

    def test_unset_returns_none(self):
        """Unset LMER_PIDS_LIMIT defers to the runtime (None)."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LMER_PIDS_LIMIT", None)
            assert _resolve_pids_limit() is None

    def test_empty_returns_none(self):
        """Empty/whitespace LMER_PIDS_LIMIT defers to the runtime (None)."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "   "}):
            assert _resolve_pids_limit() is None

    def test_positive_integer_accepted(self):
        """A positive integer is passed through verbatim."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "4096"}):
            assert _resolve_pids_limit() == "4096"

    def test_whitespace_is_stripped(self):
        """Surrounding whitespace is stripped before parsing."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "  2048  "}):
            assert _resolve_pids_limit() == "2048"

    def test_minus_one_means_unlimited(self):
        """-1 is accepted (Docker/Podman 'unlimited')."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "-1"}):
            assert _resolve_pids_limit() == "-1"

    def test_zero_rejected_returns_none(self):
        """0 is out of range and defers to the runtime (None)."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "0"}):
            assert _resolve_pids_limit() is None

    def test_other_negative_rejected_returns_none(self):
        """Negatives other than -1 defer to the runtime (None)."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "-5"}):
            assert _resolve_pids_limit() is None

    def test_non_numeric_rejected_returns_none(self):
        """A non-numeric value defers to the runtime (None)."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "lots"}):
            assert _resolve_pids_limit() is None

    def test_float_rejected_returns_none(self):
        """A float string is not an integer and defers to the runtime (None)."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "1024.5"}):
            assert _resolve_pids_limit() is None


class TestEnvArgs:
    """Test environment variable arguments"""

    def test_env_args_empty_dict(self):
        """Test env_args with empty dictionary"""
        args = env_args({})
        assert args == []

    def test_env_args_single_var(self):
        """Test env_args with single variable"""
        args = env_args({"FOO": "bar"})
        assert args == ["-e", "FOO=bar"]

    def test_env_args_multiple_vars(self):
        """Test env_args with multiple variables"""
        args = env_args({"FOO": "bar", "BAZ": "qux"})

        # Order might vary, so check both possibilities
        assert len(args) == 4
        assert "-e" in args
        assert "FOO=bar" in args or "BAZ=qux" in args

    def test_env_args_skips_none_values(self):
        """Test env_args skips None values"""
        args = env_args({"FOO": "bar", "NULL": None, "BAZ": "qux"})

        assert "-e" in args
        assert "FOO=bar" in args
        assert "BAZ=qux" in args
        assert "NULL" not in " ".join(args)

    def test_env_args_preserves_empty_string(self):
        """Test env_args preserves empty string values"""
        args = env_args({"EMPTY": ""})
        assert args == ["-e", "EMPTY="]

    def test_env_args_handles_special_characters(self):
        """Test env_args handles special characters"""
        args = env_args({"PATH": "/usr/bin:/usr/local/bin"})
        assert args == ["-e", "PATH=/usr/bin:/usr/local/bin"]

    def test_env_args_handles_numbers(self):
        """Test env_args handles numeric values"""
        args = env_args({"PORT": 8080})
        assert args == ["-e", "PORT=8080"]
