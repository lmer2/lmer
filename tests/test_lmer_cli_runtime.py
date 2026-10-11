#!/usr/bin/env python3
"""Tests for lmer_cli.runtime module"""

import csv
import os
import pytest
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

from lmer_cli import cli
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
    build_container_env,
    _available_controllers,
    _resolve_cpus,
    _resolve_limit_env,
    _resolve_memory,
    _resolve_gpu,
    _resolve_pids_limit,
    GPUGrant,
    gpu_grant_plan,
    LaunchScope,
    resource_env_names,
    resource_limit_plan,
    _user_cgroup_controllers_path,
    DEFAULT_CPUS,
    DEFAULT_MEMORY,
    DEFAULT_PIDS_LIMIT,
)


class TestDetectRuntime:
    """Test container runtime detection"""

    @pytest.fixture(autouse=True)
    def _no_override(self, monkeypatch):
        monkeypatch.delenv("LMER_CONTAINER_RUNTIME", raising=False)

    def test_override_picks_podman_when_both_available(self, monkeypatch):
        monkeypatch.setenv("LMER_CONTAINER_RUNTIME", "podman")
        with patch("shutil.which", return_value="/usr/bin/runtime"):
            assert detect_runtime() == "podman"

    def test_override_is_case_and_whitespace_insensitive(self, monkeypatch):
        monkeypatch.setenv("LMER_CONTAINER_RUNTIME", " Docker ")
        with patch("shutil.which", return_value="/usr/bin/runtime"):
            assert detect_runtime() == "docker"

    def test_empty_override_falls_back_to_detection(self, monkeypatch):
        monkeypatch.setenv("LMER_CONTAINER_RUNTIME", "")
        with patch("shutil.which", return_value="/usr/bin/runtime"):
            assert detect_runtime() == "docker"

    def test_override_rejects_unknown_runtime(self, monkeypatch):
        monkeypatch.setenv("LMER_CONTAINER_RUNTIME", "nerdctl")
        with patch("shutil.which", return_value="/usr/bin/runtime"):
            with pytest.raises(RuntimeErrorDetect, match="not supported"):
                detect_runtime()

    def test_override_missing_binary_does_not_fall_back(self, monkeypatch):
        monkeypatch.setenv("LMER_CONTAINER_RUNTIME", "podman")
        with patch("shutil.which") as mock_which:
            mock_which.side_effect = lambda x: "/usr/bin/docker" if x == "docker" else None
            with pytest.raises(RuntimeErrorDetect, match="podman is not on PATH"):
                detect_runtime()

    def test_build_takes_the_override_from_env_files(self, monkeypatch, tmp_path):
        """`lmer build` runs before main()'s .env load, yet must build into the
        store sessions run from; only the runtime key is loaded."""
        (tmp_path / ".env").write_text(
            "LMER_CONTAINER_RUNTIME=podman\nLMER_BUILD_ENV_PROBE=x\n"
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("LMER_BUILD_ENV_PROBE", raising=False)
        monkeypatch.setenv("LMER_IMAGE", "img:test")
        seen = {}

        def fake_detect():
            seen["runtime"] = os.environ.get("LMER_CONTAINER_RUNTIME")
            return "podman"

        # patch.dict: the load writes os.environ directly, which monkeypatch
        # cannot undo for a variable that was absent when it was deleted.
        with patch.dict(os.environ), \
             patch.object(cli, "detect_runtime", side_effect=fake_detect), \
             patch.object(cli, "build_image", return_value=True):
            assert cli._handle_build([]) == 0
            assert "LMER_BUILD_ENV_PROBE" not in os.environ
        assert seen["runtime"] == "podman"
        assert "LMER_CONTAINER_RUNTIME" not in os.environ

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

    @pytest.fixture(autouse=True)
    def _full_controllers(self, monkeypatch):
        """Pin a rootless euid and fully delegated controllers so tests don't
        depend on CI cgroup state or on whether CI runs as root."""
        monkeypatch.setattr("lmer_cli.runtime.os.geteuid", lambda: 1000)
        monkeypatch.setattr(
            "lmer_cli.runtime._available_controllers",
            lambda: {"cpu", "memory", "pids", "io", "cpuset"},
        )

    def test_base_args_docker(self):
        """Test base args for Docker"""
        # Isolate the resource-limit overrides from the ambient environment
        # (they may be exported from a developer's .env / host into the dev
        # container) so the default assertions below are deterministic. See
        # issue #63.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LMER_PIDS_LIMIT", None)
            os.environ.pop("LMER_CPUS", None)
            os.environ.pop("LMER_MEMORY", None)
            args = base_run_args("docker", False, "developer")

        assert "docker" in args
        assert "run" in args
        assert "--rm" in args
        # PID 1 init: reaps zombies now that clone_and_exec.py keeps the
        # runner as a child (session-end backstop) instead of exec'ing it.
        assert "--init" in args
        assert "--cpus" in args
        assert "1" in args
        assert "--memory" in args
        assert "2g" in args
        assert "--pids-limit" in args
        # Default cap is 512 and must directly follow the flag.
        assert args[args.index("--pids-limit") + 1] == "512"
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

    def test_base_args_podman_nonroot_numeric_user_includes_userns(self):
        args = base_run_args("podman", False, "1000:1000")

        assert "--userns=keep-id" in args

    def test_base_args_podman_root_user_skips_userns(self):
        """Root under the default rootless map already IS the host uid, so
        keep-id (and its whole-rootfs chown, issue #351) is skipped."""
        for user in ("0:0", "0", "root", "root:root"):
            args = base_run_args("podman", False, user)
            assert "--userns=keep-id" not in args, user
            assert args[args.index("--user") + 1] == user

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
        """LMER_PIDS_LIMIT overrides the --pids-limit value in base args."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "4096"}):
            args = base_run_args("docker", False, "developer")
            assert args[args.index("--pids-limit") + 1] == "4096"

    def test_base_args_invalid_pids_limit_falls_back_to_default(self):
        """An invalid LMER_PIDS_LIMIT leaves the default cap in place."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "0"}):
            args = base_run_args("docker", False, "developer")
            assert args[args.index("--pids-limit") + 1] == DEFAULT_PIDS_LIMIT

    def test_base_args_honors_cpus_override(self):
        """LMER_CPUS overrides the --cpus value in base args."""
        with patch.dict(os.environ, {"LMER_CPUS": "8"}):
            args = base_run_args("docker", False, "developer")
            assert args[args.index("--cpus") + 1] == "8"

    def test_base_args_invalid_cpus_falls_back_to_default(self):
        """An invalid LMER_CPUS leaves the default CPU bound in place."""
        with patch.dict(os.environ, {"LMER_CPUS": "all"}):
            args = base_run_args("docker", False, "developer")
            assert args[args.index("--cpus") + 1] == DEFAULT_CPUS

    def test_base_args_honors_memory_override(self):
        """LMER_MEMORY overrides the --memory value in base args."""
        with patch.dict(os.environ, {"LMER_MEMORY": "8g"}):
            args = base_run_args("docker", False, "developer")
            assert args[args.index("--memory") + 1] == "8g"

    def test_base_args_invalid_memory_falls_back_to_default(self):
        """An invalid LMER_MEMORY leaves the default memory bound in place."""
        with patch.dict(os.environ, {"LMER_MEMORY": "lots"}):
            args = base_run_args("docker", False, "developer")
            assert args[args.index("--memory") + 1] == DEFAULT_MEMORY

    def test_base_args_use_the_announced_gpu_plan(self):
        grant = GPUGrant("0", "LMER_GPU", ("--gpus", "device=0"))
        args = base_run_args("docker", False, "developer", gpu=grant)
        assert args[args.index("--gpus") + 1] == "device=0"


class TestResolvePidsLimit:
    """Test LMER_PIDS_LIMIT parsing for the container --pids-limit value."""

    def test_unset_returns_default(self):
        """Unset LMER_PIDS_LIMIT yields the default cap."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LMER_PIDS_LIMIT", None)
            assert _resolve_pids_limit()[0] == DEFAULT_PIDS_LIMIT

    def test_empty_returns_default(self):
        """Empty/whitespace LMER_PIDS_LIMIT yields the default cap."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "   "}):
            assert _resolve_pids_limit()[0] == DEFAULT_PIDS_LIMIT

    def test_positive_integer_accepted(self):
        """A positive integer is passed through verbatim."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "4096"}):
            assert _resolve_pids_limit()[0] == "4096"

    def test_whitespace_is_stripped(self):
        """Surrounding whitespace is stripped before parsing."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "  2048  "}):
            assert _resolve_pids_limit()[0] == "2048"

    def test_minus_one_means_unlimited(self):
        """-1 is accepted (Docker/Podman 'unlimited')."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "-1"}):
            assert _resolve_pids_limit()[0] == "-1"

    def test_zero_rejected_falls_back(self):
        """0 is out of range and falls back to the default."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "0"}):
            assert _resolve_pids_limit()[0] == DEFAULT_PIDS_LIMIT

    def test_other_negative_rejected_falls_back(self):
        """Negatives other than -1 fall back to the default."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "-5"}):
            assert _resolve_pids_limit()[0] == DEFAULT_PIDS_LIMIT

    def test_non_numeric_rejected_falls_back(self):
        """A non-numeric value falls back to the default."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "lots"}):
            assert _resolve_pids_limit()[0] == DEFAULT_PIDS_LIMIT

    def test_float_rejected_falls_back(self):
        """A float string is not an integer and falls back to the default."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "1024.5"}):
            assert _resolve_pids_limit()[0] == DEFAULT_PIDS_LIMIT

    def test_accepted_value_is_normalised(self):
        """An in-grammar spelling reaches the runtime as the integer it names,
        not as typed — the validator returns a value, not a verdict."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "+5"}):
            assert _resolve_pids_limit()[0] == "5"

    def test_invalid_value_warns(self, capsys):
        """The rejection is loud: it names the variable and the default used."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "0"}):
            _resolve_pids_limit()
        out = capsys.readouterr().out
        assert "LMER_PIDS_LIMIT" in out
        assert DEFAULT_PIDS_LIMIT in out


class TestResolveCpus:
    """Test LMER_CPUS parsing for the container --cpus value."""

    def test_unset_returns_default(self):
        """Unset LMER_CPUS yields the default core count."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LMER_CPUS", None)
            assert _resolve_cpus()[0] == DEFAULT_CPUS

    def test_empty_returns_default(self):
        """Empty/whitespace LMER_CPUS yields the default core count."""
        with patch.dict(os.environ, {"LMER_CPUS": "   "}):
            assert _resolve_cpus()[0] == DEFAULT_CPUS

    def test_positive_integer_accepted(self):
        """A positive integer is passed through verbatim."""
        with patch.dict(os.environ, {"LMER_CPUS": "2"}):
            assert _resolve_cpus()[0] == "2"

    def test_fraction_below_one_accepted(self):
        """A fractional value below one core is valid to both runtimes."""
        with patch.dict(os.environ, {"LMER_CPUS": "0.5"}):
            assert _resolve_cpus()[0] == "0.5"

    def test_fraction_above_one_accepted(self):
        """A fractional value above one core is passed through verbatim."""
        with patch.dict(os.environ, {"LMER_CPUS": "1.5"}):
            assert _resolve_cpus()[0] == "1.5"

    def test_whitespace_is_stripped(self):
        """Surrounding whitespace is stripped before parsing."""
        with patch.dict(os.environ, {"LMER_CPUS": "  4  "}):
            assert _resolve_cpus()[0] == "4"

    def test_zero_rejected_falls_back(self):
        """0 would mean no quota at all and falls back to the default."""
        with patch.dict(os.environ, {"LMER_CPUS": "0"}):
            assert _resolve_cpus()[0] == DEFAULT_CPUS

    def test_negative_rejected_falls_back(self):
        """A negative value falls back to the default (no 'unlimited' spelling)."""
        with patch.dict(os.environ, {"LMER_CPUS": "-2"}):
            assert _resolve_cpus()[0] == DEFAULT_CPUS

    def test_non_numeric_rejected_falls_back(self):
        """A non-numeric value falls back to the default."""
        with patch.dict(os.environ, {"LMER_CPUS": "abc"}):
            assert _resolve_cpus()[0] == DEFAULT_CPUS

    def test_infinity_rejected_falls_back(self):
        """'inf' parses as a float in Python but is not a core count."""
        with patch.dict(os.environ, {"LMER_CPUS": "inf"}):
            assert _resolve_cpus()[0] == DEFAULT_CPUS

    def test_nan_rejected_falls_back(self):
        """'nan' parses as a float in Python but is not a core count."""
        with patch.dict(os.environ, {"LMER_CPUS": "nan"}):
            assert _resolve_cpus()[0] == DEFAULT_CPUS

    def test_scientific_notation_rejected_falls_back(self):
        """Scientific notation is not part of the accepted grammar."""
        with patch.dict(os.environ, {"LMER_CPUS": "1e2"}):
            assert _resolve_cpus()[0] == DEFAULT_CPUS

    def test_non_ascii_digits_rejected_falls_back(self):
        """Python's \\d matches non-ASCII digits; the runtimes don't."""
        with patch.dict(os.environ, {"LMER_CPUS": "٣"}):  # Arabic-Indic 3
            assert _resolve_cpus()[0] == DEFAULT_CPUS

    def test_excess_precision_rejected_falls_back(self):
        """docker's nano-CPU parser rejects >9 fractional digits as 'too
        precise'; pre-screen those rather than abort the launch."""
        with patch.dict(os.environ, {"LMER_CPUS": "1.0000000001"}):
            assert _resolve_cpus()[0] == DEFAULT_CPUS

    def test_nanocpu_wrap_magnitude_rejected_falls_back(self):
        """2**64 nano-CPUs spelled as cores wraps docker's int64 encoding to
        0 — byte-identical to no --cpus at all. The magnitude bound exists so
        no accepted value can resolve to the unset encoding."""
        with patch.dict(os.environ, {"LMER_CPUS": "18446744073.709551616"}):
            assert _resolve_cpus()[0] == DEFAULT_CPUS

    def test_max_cpus_boundary_accepted(self):
        """The bound itself is a valid core count."""
        with patch.dict(os.environ, {"LMER_CPUS": "4096"}):
            assert _resolve_cpus()[0] == "4096"

    def test_above_max_cpus_rejected_falls_back(self):
        """Anything past the bound falls back, however far from the wrap."""
        with patch.dict(os.environ, {"LMER_CPUS": "4097"}):
            assert _resolve_cpus()[0] == DEFAULT_CPUS

    def test_invalid_value_warns(self, capsys):
        """The rejection is loud: it names the variable and the default used."""
        with patch.dict(os.environ, {"LMER_CPUS": "abc"}):
            _resolve_cpus()
        out = capsys.readouterr().out
        assert "LMER_CPUS" in out
        assert DEFAULT_CPUS in out


class TestResolveMemory:
    """Test LMER_MEMORY parsing for the container --memory value."""

    def test_unset_returns_default(self):
        """Unset LMER_MEMORY yields the default cap."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LMER_MEMORY", None)
            assert _resolve_memory()[0] == DEFAULT_MEMORY

    def test_empty_returns_default(self):
        """Empty/whitespace LMER_MEMORY yields the default cap."""
        with patch.dict(os.environ, {"LMER_MEMORY": "   "}):
            assert _resolve_memory()[0] == DEFAULT_MEMORY

    def test_gigabyte_suffix_accepted(self):
        """A size with a 'g' suffix is passed through verbatim."""
        with patch.dict(os.environ, {"LMER_MEMORY": "4g"}):
            assert _resolve_memory()[0] == "4g"

    def test_megabyte_suffix_accepted(self):
        """A size with an 'm' suffix is passed through verbatim."""
        with patch.dict(os.environ, {"LMER_MEMORY": "512m"}):
            assert _resolve_memory()[0] == "512m"

    def test_bare_integer_accepted(self):
        """A suffix-less value is bytes to both runtimes and is accepted."""
        with patch.dict(os.environ, {"LMER_MEMORY": "1073741824"}):  # 1 GiB
            assert _resolve_memory()[0] == "1073741824"

    def test_below_floor_bytes_rejected_falls_back(self):
        """A bare 8 is eight bytes — the runtime would refuse to start; the
        floor pre-screens it to warn-and-default instead."""
        with patch.dict(os.environ, {"LMER_MEMORY": "8"}):
            assert _resolve_memory()[0] == DEFAULT_MEMORY

    def test_below_floor_suffixed_rejected_falls_back(self):
        """Suffixed sizes below docker's 6m minimum are rejected too."""
        with patch.dict(os.environ, {"LMER_MEMORY": "5m"}):
            assert _resolve_memory()[0] == DEFAULT_MEMORY

    def test_floor_boundary_accepted(self):
        """Exactly the runtime minimum (6m) is a valid size."""
        with patch.dict(os.environ, {"LMER_MEMORY": "6m"}):
            assert _resolve_memory()[0] == "6m"

    def test_suffix_is_case_insensitive(self):
        """An uppercase suffix is accepted and passed through unchanged."""
        with patch.dict(os.environ, {"LMER_MEMORY": "2G"}):
            assert _resolve_memory()[0] == "2G"

    def test_two_letter_suffix_accepted(self):
        """The two-letter suffix forms ('gb', 'mb', 'kb') are valid too."""
        with patch.dict(os.environ, {"LMER_MEMORY": "1gb"}):
            assert _resolve_memory()[0] == "1gb"

    def test_whitespace_is_stripped(self):
        """Surrounding whitespace is stripped before parsing."""
        with patch.dict(os.environ, {"LMER_MEMORY": "  8g  "}):
            assert _resolve_memory()[0] == "8g"

    def test_zero_rejected_falls_back(self):
        """0 would mean no cap at all and falls back to the default."""
        with patch.dict(os.environ, {"LMER_MEMORY": "0"}):
            assert _resolve_memory()[0] == DEFAULT_MEMORY

    def test_negative_rejected_falls_back(self):
        """A negative size falls back to the default."""
        with patch.dict(os.environ, {"LMER_MEMORY": "-1g"}):
            assert _resolve_memory()[0] == DEFAULT_MEMORY

    def test_embedded_space_rejected_falls_back(self):
        """Prose spellings are rejected, not guessed at."""
        with patch.dict(os.environ, {"LMER_MEMORY": "2 gigs"}):
            assert _resolve_memory()[0] == DEFAULT_MEMORY

    def test_leading_suffix_rejected_falls_back(self):
        """A suffix before the number is not the accepted grammar."""
        with patch.dict(os.environ, {"LMER_MEMORY": "g2"}):
            assert _resolve_memory()[0] == DEFAULT_MEMORY

    def test_fraction_rejected_falls_back(self):
        """Fractions are outside the accepted subset (deliberately narrower
        than the runtimes' own size parser, which does take 2.5g)."""
        with patch.dict(os.environ, {"LMER_MEMORY": "2.5g"}):
            assert _resolve_memory()[0] == DEFAULT_MEMORY

    def test_non_ascii_digits_rejected_falls_back(self):
        """Python's \\d matches non-ASCII digits; the runtimes don't."""
        with patch.dict(os.environ, {"LMER_MEMORY": "٣g"}):  # Arabic-Indic 3
            assert _resolve_memory()[0] == DEFAULT_MEMORY

    def test_invalid_value_warns(self, capsys):
        """The rejection is loud: it names the variable and the default used."""
        with patch.dict(os.environ, {"LMER_MEMORY": "2 gigs"}):
            _resolve_memory()
        out = capsys.readouterr().out
        assert "LMER_MEMORY" in out
        assert DEFAULT_MEMORY in out


class TestResolveGpu:
    """GPU access is absent by default and uses the scoped resource ladder."""

    def test_unset_and_blank_disable_gpu_access(self):
        with patch.dict(os.environ, {}, clear=True):
            assert _resolve_gpu() == ("", "default")
        with patch.dict(os.environ, {"LMER_GPU": "   "}, clear=True):
            assert _resolve_gpu() == ("", "default")

    @pytest.mark.parametrize("value", ["all", "0", "0,1", "12,3,400"])
    def test_supported_spellings_are_accepted(self, value):
        with patch.dict(os.environ, {"LMER_GPU": value}, clear=True):
            assert _resolve_gpu() == (value, "LMER_GPU")

    @pytest.mark.parametrize(
        "value", ["ALL", "gpu0", "0-1", "0,", ",0", "0,,1", "٣"]
    )
    def test_other_spellings_warn_and_disable(self, value, capsys):
        with patch.dict(os.environ, {"LMER_GPU": value}, clear=True):
            assert _resolve_gpu() == ("", "default")
        out = capsys.readouterr().out
        assert "LMER_GPU" in out
        assert "leaving the resource disabled" in out

    def test_invalid_specific_value_falls_through_to_general(self, capsys):
        scope = LaunchScope("develop", "group/project")
        with patch.dict(
            os.environ,
            {"LMER_DEVELOP__GROUP_PROJECT__GPU": "many", "LMER_GPU": "0"},
            clear=True,
        ):
            assert _resolve_gpu(scope) == ("0", "LMER_GPU")
        assert "using LMER_GPU (0)" in capsys.readouterr().out

    def test_repo_value_beats_task_and_general_values(self):
        scope = LaunchScope("develop", "group/project")
        with patch.dict(
            os.environ,
            {
                "LMER_GPU": "0",
                "LMER_DEVELOP_GPU": "1",
                "LMER_DEVELOP__GROUP_PROJECT__GPU": "0,1",
            },
            clear=True,
        ):
            assert _resolve_gpu(scope) == (
                "0,1",
                "LMER_DEVELOP__GROUP_PROJECT__GPU",
            )


class TestGpuGrantPlan:
    """Runtime metadata decides whether an accepted request becomes a grant."""

    def test_disabled_request_never_reads_runtime_metadata(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("lmer_cli.runtime.subprocess.run") as run:
                assert gpu_grant_plan("docker") == GPUGrant(None, "default")
        run.assert_not_called()

    def test_docker_all_uses_the_native_flag(self):
        info = subprocess.CompletedProcess([], 0, '{"runc": {}, "nvidia": {}}', "")
        with patch.dict(os.environ, {"LMER_GPU": "all"}, clear=True):
            with patch("lmer_cli.runtime.subprocess.run", return_value=info) as run:
                grant = gpu_grant_plan("docker")
        assert grant == GPUGrant("all", "LMER_GPU", ("--gpus", "all"))
        run.assert_called_once_with(
            ["docker", "info", "--format", "{{json .Runtimes}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_docker_device_list_survives_csv_decoding(self):
        info = subprocess.CompletedProcess([], 0, '{"nvidia": {}}', "")
        with patch.dict(os.environ, {"LMER_GPU": "0,1"}, clear=True):
            with patch("lmer_cli.runtime.subprocess.run", return_value=info):
                grant = gpu_grant_plan("docker")
        options = next(csv.reader([grant.args[1]]))
        device_option = next(option for option in options if option.startswith("device="))
        assert device_option.removeprefix("device=").split(",") == ["0", "1"]

    def test_podman_uses_one_cdi_selector_per_device(self):
        info = subprocess.CompletedProcess(
            [],
            0,
            '{"host":{"discoveredDevices":['
            '{"source":"cdi","id":"nvidia.com/gpu=all"},'
            '{"source":"cdi","id":"nvidia.com/gpu=0"}]}}',
            "",
        )
        with patch.dict(os.environ, {"LMER_GPU": "0,1"}, clear=True):
            with patch("lmer_cli.runtime.subprocess.run", return_value=info):
                grant = gpu_grant_plan("podman")
        assert grant.args == (
            "--device",
            "nvidia.com/gpu=0",
            "--device",
            "nvidia.com/gpu=1",
        )

    def test_podman_all_uses_the_cdi_all_selector(self):
        info = subprocess.CompletedProcess(
            [],
            0,
            '{"host":{"discoveredDevices":['
            '{"source":"cdi","id":"nvidia.com/gpu=all"}]}}',
            "",
        )
        with patch.dict(os.environ, {"LMER_GPU": "all"}, clear=True):
            with patch("lmer_cli.runtime.subprocess.run", return_value=info) as run:
                grant = gpu_grant_plan("podman")
        assert grant.args == ("--device", "nvidia.com/gpu=all")
        run.assert_called_once_with(
            ["podman", "info", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_podman_requires_a_cdi_discovered_device(self, capsys):
        info = subprocess.CompletedProcess(
            [],
            0,
            '{"host":{"discoveredDevices":['
            '{"source":"other","id":"nvidia.com/gpu=all"}]}}',
            "",
        )
        with patch.dict(os.environ, {"LMER_GPU": "all"}, clear=True):
            with patch("lmer_cli.runtime.subprocess.run", return_value=info):
                grant = gpu_grant_plan("podman")
        assert grant.value is None
        assert "launching without GPU access" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("runtime", "stdout", "expected"),
        [
            ("docker", '{"runc": {}}', "nvidia runtime"),
            ("podman", '{"host":{"discoveredDevices":[]}}', "CDI device"),
        ],
    )
    def test_missing_support_drops_the_grant_with_remediation(
        self, runtime, stdout, expected, capsys
    ):
        info = subprocess.CompletedProcess([], 0, stdout, "")
        with patch.dict(os.environ, {"LMER_GPU": "0"}, clear=True):
            with patch("lmer_cli.runtime.subprocess.run", return_value=info):
                grant = gpu_grant_plan(runtime)
        assert grant.value is None and grant.args == ()
        assert grant.source == "LMER_GPU; unsupported"
        out = capsys.readouterr().out
        assert expected in out
        assert "launching without GPU access" in out
        assert "NVIDIA" in out

    def test_unreadable_metadata_drops_the_grant(self, capsys):
        info = subprocess.CompletedProcess([], 1, "", "daemon unavailable")
        with patch.dict(os.environ, {"LMER_GPU": "all"}, clear=True):
            with patch("lmer_cli.runtime.subprocess.run", return_value=info):
                grant = gpu_grant_plan("docker")
        assert grant.value is None
        assert "docker info exited 1" in capsys.readouterr().out

    def test_malformed_metadata_drops_the_grant(self, capsys):
        info = subprocess.CompletedProcess([], 0, "not-json", "")
        with patch.dict(os.environ, {"LMER_GPU": "all"}, clear=True):
            with patch("lmer_cli.runtime.subprocess.run", return_value=info):
                grant = gpu_grant_plan("podman")
        assert grant.value is None
        assert "podman info returned malformed JSON" in capsys.readouterr().out


class TestResolveLimitEnv:
    """The shared skeleton the three limit resolvers are built on (issue #271).

    Per-limit grammars are covered by the classes above; these pin the seam
    the consolidation introduced — what a validator's return value means, and
    that rejection is still warn-and-default rather than an abort.
    """

    def test_unset_does_not_consult_the_validator(self):
        """No override is not a misconfiguration: the default is used and the
        grammar is never asked, so an unset variable cannot warn."""
        consulted = []

        def validator(raw):
            consulted.append(raw)
            return raw

        with patch.dict(os.environ, {"LMER_FAKE_LIMIT": "   "}):
            assert _resolve_limit_env(["LMER_FAKE_LIMIT"], "7", validator, "hint") == ("7", "default")
        assert consulted == []

    def test_validator_return_value_is_what_reaches_the_runtime(self):
        """The validator returns the value to pass, not a verdict: one that
        normalises (as _valid_pids_limit does) must not be reduced to
        pass/fail, or '+5' would reach the runtime as typed."""
        with patch.dict(os.environ, {"LMER_FAKE_LIMIT": "  +5  "}):
            resolved = _resolve_limit_env(
                ["LMER_FAKE_LIMIT"], "7", lambda raw: str(int(raw)), "hint"
            )
        assert resolved == ("5", "LMER_FAKE_LIMIT")

    def test_rejection_warns_and_defaults(self, capsys):
        """None means rejected — warn and fall back, never abort the launch and
        never pass the value through. The warning carries the variable, what
        was read, the grammar it failed, and the default that took its place."""
        with patch.dict(os.environ, {"LMER_FAKE_LIMIT": "bogus"}):
            resolved = _resolve_limit_env(
                ["LMER_FAKE_LIMIT"], "7", lambda raw: None, "must be a positive widget"
            )
        assert resolved == ("7", "default")
        out = capsys.readouterr().out
        assert "LMER_FAKE_LIMIT" in out
        assert "'bogus'" in out
        assert "must be a positive widget" in out
        assert "the default 7" in out


class TestContainerEnv:
    """The container env transport (issue #158).

    These are the direct successors of the old ``env_args`` cases: the same
    inputs, now asserted against the env file the transport writes instead of
    the ``-e NAME=value`` argv pairs it replaced. Transport behaviour beyond
    this table lives in ``tests/test_container_env_transport.py``.
    """

    def _lines(self, container_env):
        """The env file's lines, or [] when no file was written."""
        if container_env.env_file_dir is None:
            return []
        path = container_env.env_file_dir / "container.env"
        return path.read_text(encoding="utf-8").splitlines()

    def test_empty_dict_writes_no_file(self):
        """An empty env dict produces no args and no env file at all."""
        container_env = build_container_env({})
        try:
            assert container_env.args == []
            assert container_env.env_file_dir is None
            assert container_env.subprocess_env() is None
        finally:
            container_env.cleanup()

    def test_single_var_rides_the_env_file(self):
        """A single variable becomes one --env-file line, never argv."""
        container_env = build_container_env({"FOO": "bar"})
        try:
            assert container_env.args[0] == "--env-file"
            assert self._lines(container_env) == ["FOO=bar"]
            assert "bar" not in " ".join(container_env.args[1:])
        finally:
            container_env.cleanup()

    def test_multiple_vars_all_ride_one_file(self):
        """Every variable lands in the same file; only one flag pair is added."""
        container_env = build_container_env({"FOO": "bar", "BAZ": "qux"})
        try:
            assert len(container_env.args) == 2
            assert sorted(self._lines(container_env)) == ["BAZ=qux", "FOO=bar"]
        finally:
            container_env.cleanup()

    def test_skips_none_values(self):
        """None values are skipped, exactly as env_args did."""
        container_env = build_container_env({"FOO": "bar", "NULL": None, "BAZ": "qux"})
        try:
            lines = self._lines(container_env)
            assert "FOO=bar" in lines
            assert "BAZ=qux" in lines
            assert not any(line.startswith("NULL") for line in lines)
        finally:
            container_env.cleanup()

    def test_preserves_empty_string(self):
        """An empty value stays a set-but-empty variable."""
        container_env = build_container_env({"EMPTY": ""})
        try:
            assert self._lines(container_env) == ["EMPTY="]
        finally:
            container_env.cleanup()

    def test_handles_special_characters(self):
        """A colon-separated path value survives verbatim."""
        container_env = build_container_env({"PATH": "/usr/bin:/usr/local/bin"})
        try:
            assert self._lines(container_env) == ["PATH=/usr/bin:/usr/local/bin"]
        finally:
            container_env.cleanup()

    def test_handles_numbers(self):
        """Non-string values are stringified, exactly as env_args did."""
        container_env = build_container_env({"PORT": 8080})
        try:
            assert self._lines(container_env) == ["PORT=8080"]
        finally:
            container_env.cleanup()


class TestAvailableControllers:
    """Read cgroup v2 controllers delegated to user@<uid>.service.

    A missing/unreadable file (cgroup v1, root, permission error) returns
    None — the gate does not apply and callers KEEP the resource flags.
    Only a readable v2 user-slice file returns a set, possibly empty
    (delegation known-empty — the crun-abort case the gate exists for).
    """

    def test_returns_set_from_cgroup_file(self, tmp_path, monkeypatch):
        fake = tmp_path / "cgroup.controllers"
        fake.write_text("cpuset cpu io memory pids\n")
        monkeypatch.setattr(
            "lmer_cli.runtime._user_cgroup_controllers_path",
            lambda: fake,
        )
        assert _available_controllers() == {"cpuset", "cpu", "io", "memory", "pids"}

    def test_returns_none_when_missing(self, tmp_path, monkeypatch):
        """Missing file means the gate does not apply (cgroup v1, root) —
        None, NOT an empty set, so callers keep the resource flags."""
        missing = tmp_path / "does-not-exist"
        monkeypatch.setattr(
            "lmer_cli.runtime._user_cgroup_controllers_path",
            lambda: missing,
        )
        assert _available_controllers() is None

    def test_returns_none_on_read_error(self, tmp_path, monkeypatch):
        fake = tmp_path / "cgroup.controllers"
        fake.write_text("cpu memory pids\n")
        # Simulate a Path whose .exists() succeeds but .read_text() raises.
        original_read_text = type(fake).read_text

        def boom(self, *args, **kwargs):
            raise PermissionError("nope")

        monkeypatch.setattr(type(fake), "read_text", boom)
        monkeypatch.setattr(
            "lmer_cli.runtime._user_cgroup_controllers_path",
            lambda: fake,
        )
        assert _available_controllers() is None
        monkeypatch.setattr(type(fake), "read_text", original_read_text)

    def test_path_uses_current_uid(self, monkeypatch):
        monkeypatch.setattr("lmer_cli.runtime.os.getuid", lambda: 4242)
        p = _user_cgroup_controllers_path()
        assert str(p) == (
            "/sys/fs/cgroup/user.slice/user-4242.slice/"
            "user@4242.service/cgroup.controllers"
        )


class TestBaseRunArgsCgroupGating:
    """base_run_args drops podman resource flags when controllers aren't delegated."""

    @pytest.fixture(autouse=True)
    def _rootless(self, monkeypatch):
        """The gate only applies to rootless podman; pin a non-root euid so
        these tests exercise it regardless of the CI user."""
        monkeypatch.setattr("lmer_cli.runtime.os.geteuid", lambda: 1000)

    def test_podman_with_all_controllers_includes_all_flags(self, monkeypatch):
        monkeypatch.setattr(
            "lmer_cli.runtime._available_controllers",
            lambda: {"cpu", "memory", "pids", "io", "cpuset"},
        )
        args = base_run_args("podman", False, "developer")
        assert "--cpus" in args
        assert "--memory" in args
        assert "--pids-limit" in args

    def test_podman_without_cpu_drops_cpus_flag(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "lmer_cli.runtime._available_controllers",
            lambda: {"memory", "pids"},
        )
        args = base_run_args("podman", False, "developer")
        assert "--cpus" not in args
        assert "--memory" in args
        assert "--pids-limit" in args
        captured = capsys.readouterr()
        # warning() goes to stdout in this codebase; assert it mentions cpu
        assert "cpu" in captured.out

    def test_podman_with_no_controllers_drops_all_resource_flags(self, monkeypatch):
        monkeypatch.setattr(
            "lmer_cli.runtime._available_controllers",
            lambda: set(),
        )
        args = base_run_args("podman", False, "developer")
        assert "--cpus" not in args
        assert "--memory" not in args
        assert "--pids-limit" not in args

    def test_podman_warning_lists_dropped_controllers(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "lmer_cli.runtime._available_controllers",
            lambda: {"memory"},  # cpu and pids dropped
        )
        base_run_args("podman", False, "developer")
        out = capsys.readouterr().out
        assert "cpu" in out
        assert "pids" in out
        # the hint should point at the systemd drop-in fix
        assert "user@" in out and "Delegate=" in out

    def test_podman_no_warning_when_all_controllers_present(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "lmer_cli.runtime._available_controllers",
            lambda: {"cpu", "memory", "pids"},
        )
        base_run_args("podman", False, "developer")
        out = capsys.readouterr().out
        assert "not delegated" not in out

    def test_docker_keeps_all_flags_regardless_of_controllers(self, monkeypatch):
        """Docker uses the root daemon; user-slice cgroup state is irrelevant."""
        monkeypatch.setattr(
            "lmer_cli.runtime._available_controllers",
            lambda: set(),
        )
        args = base_run_args("docker", False, "developer")
        assert "--cpus" in args
        assert "--memory" in args
        assert "--pids-limit" in args

    def test_podman_gate_not_applicable_keeps_all_flags(self, monkeypatch):
        """_available_controllers() -> None (cgroup v1, unreadable slice)
        means prior behavior: pass every resource flag, no warning."""
        monkeypatch.setattr(
            "lmer_cli.runtime._available_controllers",
            lambda: None,
        )
        args = base_run_args("podman", False, "developer")
        assert "--cpus" in args
        assert "--memory" in args
        assert "--pids-limit" in args

    def test_podman_as_root_skips_gate_and_keeps_flags(self, monkeypatch, capsys):
        """Root podman has the controllers at the root cgroup; the user-slice
        delegation gate must not apply (and must not warn about a user-slice
        drop-in that would change nothing)."""
        monkeypatch.setattr("lmer_cli.runtime.os.geteuid", lambda: 0)
        monkeypatch.setattr(
            "lmer_cli.runtime._available_controllers",
            lambda: set(),  # even a worst-case reading must be ignored for root
        )
        args = base_run_args("podman", False, "developer")
        assert "--cpus" in args
        assert "--memory" in args
        assert "--pids-limit" in args
        assert "not delegated" not in capsys.readouterr().out

    def test_podman_pids_flag_honors_lmer_pids_limit(self, monkeypatch):
        """The gated podman path must pass the RESOLVED LMER_PIDS_LIMIT value,
        not a hardcoded 512 (regression guard for the reconciliation with
        _resolve_pids_limit)."""
        monkeypatch.setattr(
            "lmer_cli.runtime._available_controllers",
            lambda: {"cpu", "memory", "pids"},
        )
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "4096"}):
            args = base_run_args("podman", False, "developer")
        assert args[args.index("--pids-limit") + 1] == "4096"

    def test_podman_cpus_flag_honors_lmer_cpus(self, monkeypatch):
        """The gated podman path must pass the RESOLVED LMER_CPUS value, not a
        hardcoded 1 (same regression class as the pids test above)."""
        monkeypatch.setattr(
            "lmer_cli.runtime._available_controllers",
            lambda: {"cpu", "memory", "pids"},
        )
        with patch.dict(os.environ, {"LMER_CPUS": "8"}):
            args = base_run_args("podman", False, "developer")
        assert args[args.index("--cpus") + 1] == "8"

    def test_podman_memory_flag_honors_lmer_memory(self, monkeypatch):
        """The gated podman path must pass the RESOLVED LMER_MEMORY value, not
        a hardcoded 2g (same regression class as the pids test above)."""
        monkeypatch.setattr(
            "lmer_cli.runtime._available_controllers",
            lambda: {"cpu", "memory", "pids"},
        )
        with patch.dict(os.environ, {"LMER_MEMORY": "8g"}):
            args = base_run_args("podman", False, "developer")
        assert args[args.index("--memory") + 1] == "8g"

    def test_dropped_flag_does_not_warn_about_its_override(self, monkeypatch, capsys):
        """Resolvers run lazily: when the gate drops --cpus, an invalid
        LMER_CPUS must not warn about falling back to a default that is
        never passed anyway."""
        monkeypatch.setattr(
            "lmer_cli.runtime._available_controllers",
            lambda: {"memory", "pids"},
        )
        with patch.dict(os.environ, {"LMER_CPUS": "bogus"}):
            args = base_run_args("podman", False, "developer")
        assert "--cpus" not in args
        out = capsys.readouterr().out
        assert "not delegated" in out  # the drop itself still warns
        assert "LMER_CPUS" not in out  # but not about an unused override


class TestScopedResourceVars:
    """The taskdef/repo-scoped resource ladder (issue #453).

    The ladder decides by specificity, not by the layer a value arrived
    through: exports, ``.env`` files and a preset's ``env`` have all been
    merged into ``os.environ`` before a container is built, so these cases
    drive ``os.environ`` directly — that IS the merged environment the
    resolvers read.
    """

    def test_task_and_repo_tiers_are_offered_most_specific_first(self):
        names = resource_env_names("MEMORY", LaunchScope("develop", "agents/global"))
        assert names == [
            "LMER_DEVELOP__AGENTS_GLOBAL__MEMORY",
            "LMER_DEVELOP_MEMORY",
            "LMER_MEMORY",
        ]

    def test_no_repo_drops_only_the_repo_tier(self):
        assert resource_env_names("CPUS", LaunchScope("develop", None)) == [
            "LMER_DEVELOP_CPUS",
            "LMER_CPUS",
        ]

    def test_no_scope_at_all_leaves_the_generic_name(self):
        assert resource_env_names("CPUS", None) == ["LMER_CPUS"]

    def test_separators_collapse_in_both_segments(self):
        """Taskdef ids and project paths are directory/forge names, not
        identifiers: everything outside [A-Za-z0-9] becomes an underscore."""
        names = resource_env_names("MEMORY", LaunchScope("code-review", "agents/sub/global"))
        assert names[0] == "LMER_CODE_REVIEW__AGENTS_SUB_GLOBAL__MEMORY"

    def test_an_unnameable_id_contributes_no_tier_rather_than_a_broken_name(self):
        """A taskdef with no ASCII alphanumerics has no scoped spelling at all;
        the generic variable still works for it."""
        assert resource_env_names("MEMORY", LaunchScope("レビュー", "agents/global")) == [
            "LMER_MEMORY"
        ]

    def test_task_value_beats_general_value(self):
        scope = LaunchScope("develop", "agents/global")
        with patch.dict(os.environ, {"LMER_MEMORY": "2g", "LMER_DEVELOP_MEMORY": "4g"}):
            assert _resolve_memory(scope) == ("4g", "LMER_DEVELOP_MEMORY")

    def test_repo_value_beats_task_value(self):
        scope = LaunchScope("develop", "agents/global")
        with patch.dict(os.environ, {
            "LMER_MEMORY": "2g",
            "LMER_DEVELOP_MEMORY": "4g",
            "LMER_DEVELOP__AGENTS_GLOBAL__MEMORY": "8g",
        }):
            assert _resolve_memory(scope) == ("8g", "LMER_DEVELOP__AGENTS_GLOBAL__MEMORY")

    def test_another_taskdefs_value_is_not_read(self):
        """The scoped name is per taskdef: a review setting leaves develop on
        the general value."""
        with patch.dict(os.environ, {"LMER_MEMORY": "2g", "LMER_REVIEW_MEMORY": "8g"}):
            assert _resolve_memory(LaunchScope("develop", None)) == ("2g", "LMER_MEMORY")

    def test_another_repos_value_is_not_read(self):
        scope = LaunchScope("develop", "agents/global")
        with patch.dict(os.environ, {
            "LMER_MEMORY": "2g",
            "LMER_DEVELOP__OTHER_PROJECT__MEMORY": "8g",
        }):
            assert _resolve_memory(scope) == ("2g", "LMER_MEMORY")

    def test_blank_scoped_value_falls_through_rather_than_selecting_nothing(self):
        scope = LaunchScope("develop", "agents/global")
        with patch.dict(os.environ, {"LMER_MEMORY": "2g", "LMER_DEVELOP_MEMORY": "   "}):
            assert _resolve_memory(scope) == ("2g", "LMER_MEMORY")

    def test_invalid_scoped_value_falls_through_to_the_next_candidate(self, capsys):
        """Not to the default: an operator with both set meant the general one
        to answer when the specific one cannot."""
        scope = LaunchScope("develop", "agents/global")
        with patch.dict(os.environ, {"LMER_MEMORY": "4g", "LMER_DEVELOP_MEMORY": "lots"}):
            assert _resolve_memory(scope) == ("4g", "LMER_MEMORY")
        out = capsys.readouterr().out
        assert "LMER_DEVELOP_MEMORY" in out
        # The warning names the bound that actually took effect, not the next
        # candidate it had yet to validate.
        assert "using LMER_MEMORY (4g)" in out

    def test_every_candidate_invalid_still_leaves_a_finite_bound(self, capsys):
        scope = LaunchScope("develop", "agents/global")
        with patch.dict(os.environ, {"LMER_MEMORY": "heaps", "LMER_DEVELOP_MEMORY": "lots"}):
            assert _resolve_memory(scope) == (DEFAULT_MEMORY, "default")
        out = capsys.readouterr().out
        assert "LMER_DEVELOP_MEMORY" in out and "LMER_MEMORY" in out
        # Both warnings name the default, because that is what was used — an
        # earlier version named the next SET candidate, which was then rejected
        # too, so the first warning described a bound nothing ever had.
        assert out.count(f"using the default {DEFAULT_MEMORY}") == 2

    def test_pids_limit_has_no_scoped_spelling(self):
        """Issue #453 leaves PID policy alone: a taskdef-scoped name for it is
        not read, so setting one changes nothing."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LMER_PIDS_LIMIT", None)
            os.environ["LMER_DEVELOP_PIDS_LIMIT"] = "4096"
            assert _resolve_pids_limit() == (DEFAULT_PIDS_LIMIT, "default")


class TestResourceLimitPlan:
    """One resolution behind the container arguments and the diagnostic."""

    def test_plan_reports_the_variable_that_answered(self):
        with patch.dict(os.environ, {"LMER_DEVELOP_MEMORY": "8g"}):
            plan = resource_limit_plan("docker", LaunchScope("develop", None))
        memory = next(limit for limit in plan if limit.flag == "--memory")
        assert (memory.value, memory.source) == ("8g", "LMER_DEVELOP_MEMORY")

    def test_a_flag_override_beats_every_tier_and_says_so(self):
        scope = LaunchScope("develop", "agents/global")
        with patch.dict(os.environ, {
            "LMER_MEMORY": "2g",
            "LMER_DEVELOP__AGENTS_GLOBAL__MEMORY": "8g",
        }):
            plan = resource_limit_plan("docker", scope, {"--memory": "16g"})
        memory = next(limit for limit in plan if limit.flag == "--memory")
        assert (memory.value, memory.source) == ("16g", "--memory")

    def test_an_override_of_one_knob_leaves_the_other_on_its_ladder(self):
        with patch.dict(os.environ, {"LMER_DEVELOP_CPUS": "3", "LMER_DEVELOP_MEMORY": "8g"}):
            plan = resource_limit_plan("docker", LaunchScope("develop", None), {"--cpus": "1"})
        by_flag = {limit.flag: (limit.value, limit.source) for limit in plan}
        assert by_flag["--cpus"] == ("1", "--cpus")
        assert by_flag["--memory"] == ("8g", "LMER_DEVELOP_MEMORY")

    def test_args_are_rendered_from_the_plan(self):
        """The diagnostic and the arguments read one object, so they cannot
        describe different launches."""
        with patch.dict(os.environ, {"LMER_DEVELOP_MEMORY": "8g"}):
            plan = resource_limit_plan("docker", LaunchScope("develop", None))
            args = base_run_args("docker", False, "developer", plan)
        for limit in plan:
            assert args[args.index(limit.flag) + 1] == limit.value

    def test_a_dropped_flag_is_absent_from_the_plan_and_the_args(self, monkeypatch):
        """Rootless podman without delegated controllers: what is not passed is
        not announced either."""
        monkeypatch.setattr("lmer_cli.runtime.os.geteuid", lambda: 1000)
        monkeypatch.setattr("lmer_cli.runtime._available_controllers", lambda: {"pids"})
        plan = resource_limit_plan("podman", LaunchScope("develop", None))
        assert [limit.flag for limit in plan] == ["--pids-limit"]
        args = base_run_args("podman", False, "developer", plan)
        assert "--memory" not in args and "--cpus" not in args


class TestGateCiBaselineLadder:
    """LMER_GATE_CI_BASELINE rides the #453 ladder (#452): most specific first,
    blank and invalid fall through, default off."""

    def _resolve(self, env, scope=LaunchScope("develop", "agents/global")):
        from lmer_cli.runtime import resolve_gate_ci_baseline
        with patch.dict(os.environ, env, clear=False):
            for name in [n for n in os.environ if n.endswith("GATE_CI_BASELINE") and n not in env]:
                os.environ.pop(name)
            return resolve_gate_ci_baseline(scope)

    def test_default_is_off(self):
        assert self._resolve({}) == ("0", "default")

    def test_names_follow_the_resource_derivation(self):
        assert resource_env_names("GATE_CI_BASELINE", LaunchScope("develop", "agents/global")) == [
            "LMER_DEVELOP__AGENTS_GLOBAL__GATE_CI_BASELINE",
            "LMER_DEVELOP_GATE_CI_BASELINE",
            "LMER_GATE_CI_BASELINE",
        ]

    def test_repo_tier_beats_task_and_general(self):
        assert self._resolve({
            "LMER_GATE_CI_BASELINE": "0",
            "LMER_DEVELOP_GATE_CI_BASELINE": "0",
            "LMER_DEVELOP__AGENTS_GLOBAL__GATE_CI_BASELINE": "yes",
        }) == ("1", "LMER_DEVELOP__AGENTS_GLOBAL__GATE_CI_BASELINE")

    def test_explicit_off_at_a_specific_tier_wins(self):
        assert self._resolve({
            "LMER_GATE_CI_BASELINE": "1",
            "LMER_DEVELOP__AGENTS_GLOBAL__GATE_CI_BASELINE": "false",
        }) == ("0", "LMER_DEVELOP__AGENTS_GLOBAL__GATE_CI_BASELINE")

    def test_task_tier_applies_to_any_repository(self):
        assert self._resolve({"LMER_DEVELOP_GATE_CI_BASELINE": "TRUE"},
                             LaunchScope("develop", "other/repo")) == (
            "1", "LMER_DEVELOP_GATE_CI_BASELINE")

    def test_blank_falls_through(self):
        assert self._resolve({
            "LMER_DEVELOP__AGENTS_GLOBAL__GATE_CI_BASELINE": "  ",
            "LMER_GATE_CI_BASELINE": "1",
        }) == ("1", "LMER_GATE_CI_BASELINE")

    def test_invalid_warns_and_falls_through_to_the_next_candidate(self, capsys):
        assert self._resolve({
            "LMER_DEVELOP_GATE_CI_BASELINE": "maybe",
            "LMER_GATE_CI_BASELINE": "1",
        }) == ("1", "LMER_GATE_CI_BASELINE")
        captured = capsys.readouterr()
        assert "LMER_DEVELOP_GATE_CI_BASELINE" in captured.out + captured.err

    def test_invalid_with_nothing_else_stays_off(self):
        assert self._resolve({"LMER_GATE_CI_BASELINE": "on"}) == ("0", "default")

    def test_no_repo_session_has_no_repo_tier(self):
        assert self._resolve({
            "LMER_DEVELOP__AGENTS_GLOBAL__GATE_CI_BASELINE": "1",
        }, LaunchScope("develop", None)) == ("0", "default")

    def test_no_task_session_reads_only_the_general_name(self):
        assert self._resolve({
            "LMER_DEVELOP_GATE_CI_BASELINE": "1",
            "LMER_GATE_CI_BASELINE": "0",
        }, None) == ("0", "LMER_GATE_CI_BASELINE")

    def test_there_is_no_repo_only_tier(self):
        assert self._resolve({"LMER_AGENTS_GLOBAL_GATE_CI_BASELINE": "1"}) == ("0", "default")
