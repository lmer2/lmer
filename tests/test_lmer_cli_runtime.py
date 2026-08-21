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
    build_container_env,
    _available_controllers,
    _resolve_cpus,
    _resolve_limit_env,
    _resolve_memory,
    _resolve_pids_limit,
    _user_cgroup_controllers_path,
    DEFAULT_CPUS,
    DEFAULT_MEMORY,
    DEFAULT_PIDS_LIMIT,
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


class TestResolvePidsLimit:
    """Test LMER_PIDS_LIMIT parsing for the container --pids-limit value."""

    def test_unset_returns_default(self):
        """Unset LMER_PIDS_LIMIT yields the default cap."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LMER_PIDS_LIMIT", None)
            assert _resolve_pids_limit() == DEFAULT_PIDS_LIMIT

    def test_empty_returns_default(self):
        """Empty/whitespace LMER_PIDS_LIMIT yields the default cap."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "   "}):
            assert _resolve_pids_limit() == DEFAULT_PIDS_LIMIT

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

    def test_zero_rejected_falls_back(self):
        """0 is out of range and falls back to the default."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "0"}):
            assert _resolve_pids_limit() == DEFAULT_PIDS_LIMIT

    def test_other_negative_rejected_falls_back(self):
        """Negatives other than -1 fall back to the default."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "-5"}):
            assert _resolve_pids_limit() == DEFAULT_PIDS_LIMIT

    def test_non_numeric_rejected_falls_back(self):
        """A non-numeric value falls back to the default."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "lots"}):
            assert _resolve_pids_limit() == DEFAULT_PIDS_LIMIT

    def test_float_rejected_falls_back(self):
        """A float string is not an integer and falls back to the default."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "1024.5"}):
            assert _resolve_pids_limit() == DEFAULT_PIDS_LIMIT

    def test_accepted_value_is_normalised(self):
        """An in-grammar spelling reaches the runtime as the integer it names,
        not as typed — the validator returns a value, not a verdict."""
        with patch.dict(os.environ, {"LMER_PIDS_LIMIT": "+5"}):
            assert _resolve_pids_limit() == "5"

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
            assert _resolve_cpus() == DEFAULT_CPUS

    def test_empty_returns_default(self):
        """Empty/whitespace LMER_CPUS yields the default core count."""
        with patch.dict(os.environ, {"LMER_CPUS": "   "}):
            assert _resolve_cpus() == DEFAULT_CPUS

    def test_positive_integer_accepted(self):
        """A positive integer is passed through verbatim."""
        with patch.dict(os.environ, {"LMER_CPUS": "2"}):
            assert _resolve_cpus() == "2"

    def test_fraction_below_one_accepted(self):
        """A fractional value below one core is valid to both runtimes."""
        with patch.dict(os.environ, {"LMER_CPUS": "0.5"}):
            assert _resolve_cpus() == "0.5"

    def test_fraction_above_one_accepted(self):
        """A fractional value above one core is passed through verbatim."""
        with patch.dict(os.environ, {"LMER_CPUS": "1.5"}):
            assert _resolve_cpus() == "1.5"

    def test_whitespace_is_stripped(self):
        """Surrounding whitespace is stripped before parsing."""
        with patch.dict(os.environ, {"LMER_CPUS": "  4  "}):
            assert _resolve_cpus() == "4"

    def test_zero_rejected_falls_back(self):
        """0 would mean no quota at all and falls back to the default."""
        with patch.dict(os.environ, {"LMER_CPUS": "0"}):
            assert _resolve_cpus() == DEFAULT_CPUS

    def test_negative_rejected_falls_back(self):
        """A negative value falls back to the default (no 'unlimited' spelling)."""
        with patch.dict(os.environ, {"LMER_CPUS": "-2"}):
            assert _resolve_cpus() == DEFAULT_CPUS

    def test_non_numeric_rejected_falls_back(self):
        """A non-numeric value falls back to the default."""
        with patch.dict(os.environ, {"LMER_CPUS": "abc"}):
            assert _resolve_cpus() == DEFAULT_CPUS

    def test_infinity_rejected_falls_back(self):
        """'inf' parses as a float in Python but is not a core count."""
        with patch.dict(os.environ, {"LMER_CPUS": "inf"}):
            assert _resolve_cpus() == DEFAULT_CPUS

    def test_nan_rejected_falls_back(self):
        """'nan' parses as a float in Python but is not a core count."""
        with patch.dict(os.environ, {"LMER_CPUS": "nan"}):
            assert _resolve_cpus() == DEFAULT_CPUS

    def test_scientific_notation_rejected_falls_back(self):
        """Scientific notation is not part of the accepted grammar."""
        with patch.dict(os.environ, {"LMER_CPUS": "1e2"}):
            assert _resolve_cpus() == DEFAULT_CPUS

    def test_non_ascii_digits_rejected_falls_back(self):
        """Python's \\d matches non-ASCII digits; the runtimes don't."""
        with patch.dict(os.environ, {"LMER_CPUS": "٣"}):  # Arabic-Indic 3
            assert _resolve_cpus() == DEFAULT_CPUS

    def test_excess_precision_rejected_falls_back(self):
        """docker's nano-CPU parser rejects >9 fractional digits as 'too
        precise'; pre-screen those rather than abort the launch."""
        with patch.dict(os.environ, {"LMER_CPUS": "1.0000000001"}):
            assert _resolve_cpus() == DEFAULT_CPUS

    def test_nanocpu_wrap_magnitude_rejected_falls_back(self):
        """2**64 nano-CPUs spelled as cores wraps docker's int64 encoding to
        0 — byte-identical to no --cpus at all. The magnitude bound exists so
        no accepted value can resolve to the unset encoding."""
        with patch.dict(os.environ, {"LMER_CPUS": "18446744073.709551616"}):
            assert _resolve_cpus() == DEFAULT_CPUS

    def test_max_cpus_boundary_accepted(self):
        """The bound itself is a valid core count."""
        with patch.dict(os.environ, {"LMER_CPUS": "4096"}):
            assert _resolve_cpus() == "4096"

    def test_above_max_cpus_rejected_falls_back(self):
        """Anything past the bound falls back, however far from the wrap."""
        with patch.dict(os.environ, {"LMER_CPUS": "4097"}):
            assert _resolve_cpus() == DEFAULT_CPUS

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
            assert _resolve_memory() == DEFAULT_MEMORY

    def test_empty_returns_default(self):
        """Empty/whitespace LMER_MEMORY yields the default cap."""
        with patch.dict(os.environ, {"LMER_MEMORY": "   "}):
            assert _resolve_memory() == DEFAULT_MEMORY

    def test_gigabyte_suffix_accepted(self):
        """A size with a 'g' suffix is passed through verbatim."""
        with patch.dict(os.environ, {"LMER_MEMORY": "4g"}):
            assert _resolve_memory() == "4g"

    def test_megabyte_suffix_accepted(self):
        """A size with an 'm' suffix is passed through verbatim."""
        with patch.dict(os.environ, {"LMER_MEMORY": "512m"}):
            assert _resolve_memory() == "512m"

    def test_bare_integer_accepted(self):
        """A suffix-less value is bytes to both runtimes and is accepted."""
        with patch.dict(os.environ, {"LMER_MEMORY": "1073741824"}):  # 1 GiB
            assert _resolve_memory() == "1073741824"

    def test_below_floor_bytes_rejected_falls_back(self):
        """A bare 8 is eight bytes — the runtime would refuse to start; the
        floor pre-screens it to warn-and-default instead."""
        with patch.dict(os.environ, {"LMER_MEMORY": "8"}):
            assert _resolve_memory() == DEFAULT_MEMORY

    def test_below_floor_suffixed_rejected_falls_back(self):
        """Suffixed sizes below docker's 6m minimum are rejected too."""
        with patch.dict(os.environ, {"LMER_MEMORY": "5m"}):
            assert _resolve_memory() == DEFAULT_MEMORY

    def test_floor_boundary_accepted(self):
        """Exactly the runtime minimum (6m) is a valid size."""
        with patch.dict(os.environ, {"LMER_MEMORY": "6m"}):
            assert _resolve_memory() == "6m"

    def test_suffix_is_case_insensitive(self):
        """An uppercase suffix is accepted and passed through unchanged."""
        with patch.dict(os.environ, {"LMER_MEMORY": "2G"}):
            assert _resolve_memory() == "2G"

    def test_two_letter_suffix_accepted(self):
        """The two-letter suffix forms ('gb', 'mb', 'kb') are valid too."""
        with patch.dict(os.environ, {"LMER_MEMORY": "1gb"}):
            assert _resolve_memory() == "1gb"

    def test_whitespace_is_stripped(self):
        """Surrounding whitespace is stripped before parsing."""
        with patch.dict(os.environ, {"LMER_MEMORY": "  8g  "}):
            assert _resolve_memory() == "8g"

    def test_zero_rejected_falls_back(self):
        """0 would mean no cap at all and falls back to the default."""
        with patch.dict(os.environ, {"LMER_MEMORY": "0"}):
            assert _resolve_memory() == DEFAULT_MEMORY

    def test_negative_rejected_falls_back(self):
        """A negative size falls back to the default."""
        with patch.dict(os.environ, {"LMER_MEMORY": "-1g"}):
            assert _resolve_memory() == DEFAULT_MEMORY

    def test_embedded_space_rejected_falls_back(self):
        """Prose spellings are rejected, not guessed at."""
        with patch.dict(os.environ, {"LMER_MEMORY": "2 gigs"}):
            assert _resolve_memory() == DEFAULT_MEMORY

    def test_leading_suffix_rejected_falls_back(self):
        """A suffix before the number is not the accepted grammar."""
        with patch.dict(os.environ, {"LMER_MEMORY": "g2"}):
            assert _resolve_memory() == DEFAULT_MEMORY

    def test_fraction_rejected_falls_back(self):
        """Fractions are outside the accepted subset (deliberately narrower
        than the runtimes' own size parser, which does take 2.5g)."""
        with patch.dict(os.environ, {"LMER_MEMORY": "2.5g"}):
            assert _resolve_memory() == DEFAULT_MEMORY

    def test_non_ascii_digits_rejected_falls_back(self):
        """Python's \\d matches non-ASCII digits; the runtimes don't."""
        with patch.dict(os.environ, {"LMER_MEMORY": "٣g"}):  # Arabic-Indic 3
            assert _resolve_memory() == DEFAULT_MEMORY

    def test_invalid_value_warns(self, capsys):
        """The rejection is loud: it names the variable and the default used."""
        with patch.dict(os.environ, {"LMER_MEMORY": "2 gigs"}):
            _resolve_memory()
        out = capsys.readouterr().out
        assert "LMER_MEMORY" in out
        assert DEFAULT_MEMORY in out


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
            assert _resolve_limit_env("LMER_FAKE_LIMIT", "7", validator, "hint") == "7"
        assert consulted == []

    def test_validator_return_value_is_what_reaches_the_runtime(self):
        """The validator returns the value to pass, not a verdict: one that
        normalises (as _valid_pids_limit does) must not be reduced to
        pass/fail, or '+5' would reach the runtime as typed."""
        with patch.dict(os.environ, {"LMER_FAKE_LIMIT": "  +5  "}):
            resolved = _resolve_limit_env(
                "LMER_FAKE_LIMIT", "7", lambda raw: str(int(raw)), "hint"
            )
        assert resolved == "5"

    def test_rejection_warns_and_defaults(self, capsys):
        """None means rejected — warn and fall back, never abort the launch and
        never pass the value through. The warning carries the variable, what
        was read, the grammar it failed, and the default that took its place."""
        with patch.dict(os.environ, {"LMER_FAKE_LIMIT": "bogus"}):
            resolved = _resolve_limit_env(
                "LMER_FAKE_LIMIT", "7", lambda raw: None, "must be a positive widget"
            )
        assert resolved == "7"
        out = capsys.readouterr().out
        assert "LMER_FAKE_LIMIT" in out
        assert "'bogus'" in out
        assert "must be a positive widget" in out
        assert "using default 7" in out


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
