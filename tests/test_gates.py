#!/usr/bin/env python3
"""
Tests for the gate system and gate commands
"""

import json
import pytest
import re
import subprocess
import os
import sys
import tempfile
import shutil
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, call

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lmer_cli import gate_cache, precommit_cache
from lmer_cli.gates import GateSystem, CheckStatus, CheckResult, Colors
from tests.conftest import strip_lmer_env


def enable_precommit_reuse(monkeypatch, tmp_path):
    """Authorize a cache skip outside the repository being checked."""
    work = tmp_path / "work"
    info = work / "git.example.com" / "org/proj" / "info"
    info.mkdir(parents=True)
    info.joinpath("gate-check.yaml").write_text(
        "precommit:\n  reuse_all_files: true\n"
    )
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
    monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
    monkeypatch.setenv("LMER_REPO_PROJECT", "org/proj")


class TestGateSystem:
    """Test the GateSystem class functionality"""

    def setup_method(self):
        """Set up test environment"""
        self.gate = GateSystem(verbose=True)

    def test_init(self):
        """Test GateSystem initialization"""
        assert self.gate.verbose == True
        assert self.gate.commit_handoff is False
        assert self.gate.results == []
        # Verify project_root points to a valid project directory
        # (may differ due to bind mounts: /workspace vs /home/developer/Agents/global)
        assert self.gate.project_root.exists()
        assert (self.gate.project_root / "pyproject.toml").exists()
        assert (self.gate.project_root / "src").exists()

    def test_run_command_success(self):
        """Test running a successful command"""
        code, stdout, stderr = self.gate.run_command(["echo", "test"])
        assert code == 0
        assert stdout.strip() == "test"
        assert stderr == ""

    def test_run_command_failure(self):
        """Test running a failing command"""
        code, stdout, stderr = self.gate.run_command(["false"], check=False)
        assert code != 0

    def test_run_command_not_found(self):
        """Test running a non-existent command"""
        code, stdout, stderr = self.gate.run_command(["nonexistent_command_12345"])
        assert code == 127
        assert "Command not found" in stderr

    @patch('subprocess.run')
    def test_check_git_status_clean(self, mock_run):
        """Test checking git status when clean"""
        # Mock git diff (unstaged)
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # git diff
            MagicMock(returncode=0, stdout="", stderr=""),  # git ls-files
        ]

        result = self.gate.check_git_status()
        assert result.status == CheckStatus.PASSED
        assert result.message == "No unstaged changes"

    @patch('subprocess.run')
    def test_check_git_status_unstaged(self, mock_run):
        """Test checking git status with unstaged changes"""
        # Mock git diff (unstaged)
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="file1.py\nfile2.py", stderr=""),  # git diff
            MagicMock(returncode=0, stdout="", stderr=""),  # git ls-files
        ]

        result = self.gate.check_git_status()
        assert result.status == CheckStatus.FAILED
        assert "Unstaged or untracked changes detected" in result.message
        assert "M file1.py" in result.details

    @patch('subprocess.run')
    def test_check_staged_files_clean(self, mock_run):
        """Test checking staged files with no suspicious patterns"""
        mock_run.return_value = MagicMock(returncode=0, stdout="src/main.py\ntests/test_main.py", stderr="")

        result = self.gate.check_staged_files()
        assert result.status == CheckStatus.PASSED
        assert "2 files staged" in result.message

    @patch('subprocess.run')
    def test_check_staged_files_suspicious(self, mock_run):
        """Test checking staged files with suspicious patterns"""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="src/main.py\n.venv/lib/python3.11/site-packages/pip.py\n__pycache__/test.pyc",
            stderr=""
        )

        result = self.gate.check_staged_files()
        assert result.status == CheckStatus.FAILED
        assert "Suspicious files staged" in result.message
        assert any(".venv/" in detail for detail in result.details)

    @patch('subprocess.run')
    def test_check_staged_files_too_many(self, mock_run):
        """Test checking staged files with too many files"""
        files = "\n".join([f"file{i}.py" for i in range(25)])
        mock_run.return_value = MagicMock(returncode=0, stdout=files, stderr="")

        result = self.gate.check_staged_files()
        assert result.status == CheckStatus.WARNING
        assert "Large number of files staged" in result.message
        assert not result.is_critical

    @patch('subprocess.run')
    def test_check_branch_feature(self, mock_run):
        """Test checking branch on feature branch"""
        mock_run.return_value = MagicMock(returncode=0, stdout="feature/new-feature", stderr="")

        result = self.gate.check_branch()
        assert result.status == CheckStatus.PASSED
        assert "feature/new-feature" in result.message

    @patch('subprocess.run')
    def test_check_branch_main(self, mock_run):
        """Test checking branch on main branch"""
        mock_run.return_value = MagicMock(returncode=0, stdout="main", stderr="")

        result = self.gate.check_branch()
        assert result.status == CheckStatus.WARNING
        assert "On main branch" in result.message
        assert not result.is_critical

    @staticmethod
    def _is_probe_call(cmd):
        """True if `cmd` is an `_interpreter_can_import` probe (`-c 'import ...'`)."""
        return "-c" in cmd and any(
            isinstance(a, str) and a.startswith("import ") for a in cmd
        )

    @staticmethod
    def _is_pytest_run(cmd):
        """True if `cmd` is the actual pytest invocation (`-m pytest`)."""
        return "-m" in cmd and "pytest" in cmd

    @patch('subprocess.run')
    def test_check_tests_pass(self, mock_run):
        """Test running tests when they pass"""
        # The probe (`python -c 'import pytest'`) and the pytest run both hit
        # subprocess.run; distinguish them via side_effect.
        def side_effect(cmd, *args, **kwargs):
            if self._is_probe_call(cmd):
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="5 passed in 0.5s", stderr="")

        mock_run.side_effect = side_effect

        result = self.gate.check_tests()
        assert result.status == CheckStatus.PASSED
        assert "5 tests" in result.message
        # Full output is captured for the log even on success.
        assert result.full_output is not None
        assert "5 passed in 0.5s" in result.full_output

    @patch('subprocess.run')
    def test_check_tests_fail(self, mock_run):
        """Test running tests when they fail"""
        def side_effect(cmd, *args, **kwargs):
            if self._is_probe_call(cmd):
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(
                returncode=1,
                stdout="FAILED tests/test_main.py::test_function",
                stderr="",
            )

        mock_run.side_effect = side_effect

        result = self.gate.check_tests()
        assert result.status == CheckStatus.FAILED
        assert "Tests failed" in result.message
        # Full pytest output is preserved so the failure can be investigated
        # from the log without re-running the suite.
        assert result.full_output is not None
        assert "FAILED tests/test_main.py::test_function" in result.full_output

    @patch('subprocess.run')
    def test_check_tests_uses_venv_python_when_importable(self, mock_run, tmp_path):
        """Venv python that can import pytest is used for the pytest run."""
        (tmp_path / "tests").mkdir()
        venv_python = tmp_path / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("#!/bin/sh\n")
        self.gate.project_root = tmp_path

        def side_effect(cmd, *args, **kwargs):
            if self._is_probe_call(cmd):
                # Any interpreter probed reports pytest importable.
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="3 passed in 0.1s", stderr="")

        mock_run.side_effect = side_effect

        result = self.gate.check_tests()
        assert result.status == CheckStatus.PASSED

        pytest_calls = [
            c for c in mock_run.call_args_list if self._is_pytest_run(c.args[0])
        ]
        assert len(pytest_calls) == 1
        assert pytest_calls[0].args[0][0] == str(venv_python)

    @patch('subprocess.run')
    def test_check_tests_falls_back_when_venv_cannot_import(self, mock_run, tmp_path):
        """Venv python that cannot import pytest falls back to a PATH interpreter."""
        (tmp_path / "tests").mkdir()
        venv_python = tmp_path / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("#!/bin/sh\n")
        self.gate.project_root = tmp_path

        def side_effect(cmd, *args, **kwargs):
            if self._is_probe_call(cmd):
                # The venv python cannot import pytest; PATH interpreters can.
                if cmd[0] == str(venv_python):
                    return MagicMock(returncode=1, stdout="", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="3 passed in 0.1s", stderr="")

        mock_run.side_effect = side_effect

        result = self.gate.check_tests()
        assert result.status == CheckStatus.PASSED

        pytest_calls = [
            c for c in mock_run.call_args_list if self._is_pytest_run(c.args[0])
        ]
        assert len(pytest_calls) == 1
        chosen = pytest_calls[0].args[0][0]
        assert chosen != str(venv_python)
        assert chosen in ("python3", "python")

    @patch('subprocess.run')
    def test_check_precommit_pass(self, mock_run):
        """Test running pre-commit when it passes"""
        mock_run.return_value = MagicMock(returncode=0, stdout="All checks passed", stderr="")

        result = self.gate.check_precommit()
        assert result.status == CheckStatus.PASSED
        assert "All hooks passed" in result.message
        assert result.full_output is not None
        assert "All checks passed" in result.full_output

    @patch('subprocess.run')
    def test_check_precommit_fail(self, mock_run):
        """Pre-commit exit > 0 surfaces the raw stdout/stderr tail as details."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="black.....................................Failed\nFix it.",
            stderr="",
        )

        result = self.gate.check_precommit()
        assert result.status == CheckStatus.FAILED
        assert result.message == "pre-commit exited 1"
        # Both output lines reach the user — no parsing-out-only-"Failed" lines.
        assert result.details is not None
        assert "black.....................................Failed" in result.details
        assert "Fix it." in result.details
        # Complete output is preserved for the log file.
        assert result.full_output is not None
        assert "black.....................................Failed" in result.full_output
        assert "Fix it." in result.full_output

    @patch('subprocess.run')
    def test_check_precommit_fail_surfaces_non_precommit_output(self, mock_run):
        """Regression: when the configured command isn't actually pre-commit
        (e.g. `uv run pre-commit` printing a uv sync error before pre-commit
        runs), the raw output still surfaces instead of being silently parsed
        away. This was a mysqlclient-build failure that hid behind a generic
        'Some hooks failed' summary."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr=(
                "  × Failed to build `mysqlclient==2.2.8`\n"
                "  ╰─▶ Call to `setuptools.build_meta.build_wheel` failed "
                "(exit status: 1)\n"
            ),
        )

        result = self.gate.check_precommit()
        assert result.status == CheckStatus.FAILED
        assert any("mysqlclient" in line for line in result.details or [])
        assert any("setuptools.build_meta" in line for line in result.details or [])

    @patch('subprocess.run')
    def test_check_precommit_fail_with_empty_output(self, mock_run):
        """Empty output → at least state the exit code so the user has something."""
        mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="")

        result = self.gate.check_precommit()
        assert result.status == CheckStatus.FAILED
        assert result.details == ["pre-commit exited 2 with no output"]

    @patch.object(precommit_cache, "compute_fingerprint")
    @patch('subprocess.run')
    def test_check_precommit_reuse_is_off_by_default(
        self, mock_run, compute_fingerprint, tmp_path, monkeypatch
    ):
        strip_lmer_env(monkeypatch)
        mock_run.return_value = MagicMock(returncode=0, stdout="passed", stderr="")
        self.gate.project_root = tmp_path

        result = self.gate.check_precommit()

        assert result.status == CheckStatus.PASSED
        compute_fingerprint.assert_not_called()
        mock_run.assert_called_once()

    @patch.object(precommit_cache, "compute_fingerprint")
    @patch('subprocess.run')
    def test_repo_local_config_cannot_authorize_skipping_precommit(
        self, mock_run, compute_fingerprint, tmp_path, monkeypatch
    ):
        strip_lmer_env(monkeypatch)
        config_dir = tmp_path / ".lmer"
        config_dir.mkdir()
        config_dir.joinpath("gate-check.yaml").write_text(
            "precommit:\n  reuse_all_files: true\n"
        )
        self.gate.project_root = tmp_path
        mock_run.return_value = MagicMock(returncode=0, stdout="passed", stderr="")

        result = self.gate.check_precommit()

        assert result.status == CheckStatus.PASSED
        compute_fingerprint.assert_not_called()
        mock_run.assert_called_once()

    @patch.object(precommit_cache, "read_pass")
    @patch.object(precommit_cache, "compute_fingerprint")
    @patch('subprocess.run')
    def test_check_precommit_reuses_an_opted_in_exact_pass(
        self, mock_run, compute_fingerprint, read_pass, tmp_path, monkeypatch
    ):
        enable_precommit_reuse(monkeypatch, tmp_path)
        self.gate.project_root = tmp_path
        fingerprint = MagicMock()
        compute_fingerprint.return_value = fingerprint
        read_pass.return_value = {"created_at": time.time() - 3}

        result = self.gate.check_precommit()

        assert result.status == CheckStatus.PASSED
        assert "Reused recent" in result.message
        mock_run.assert_not_called()

    @pytest.mark.parametrize("changed_after", [False, True])
    @patch.object(precommit_cache, "record_pass")
    @patch.object(precommit_cache, "read_pass", return_value=None)
    @patch.object(precommit_cache, "compute_fingerprint")
    @patch('subprocess.run')
    def test_check_precommit_records_only_a_stable_success(
        self, mock_run, compute_fingerprint, _read_pass, record_pass,
        changed_after, tmp_path, monkeypatch
    ):
        enable_precommit_reuse(monkeypatch, tmp_path)
        self.gate.project_root = tmp_path
        before = MagicMock(name="before")
        after = MagicMock(name="after") if changed_after else before
        compute_fingerprint.side_effect = [before, after]
        mock_run.return_value = MagicMock(returncode=0, stdout="passed", stderr="")

        result = self.gate.check_precommit()

        assert result.status == CheckStatus.PASSED
        if changed_after:
            record_pass.assert_not_called()
        else:
            record_pass.assert_called_once_with(before)

    @patch.object(precommit_cache, "record_pass")
    @patch.object(precommit_cache, "read_pass", return_value=None)
    @patch.object(precommit_cache, "compute_fingerprint", return_value=MagicMock())
    @patch('subprocess.run')
    def test_check_precommit_never_records_a_failure(
        self, mock_run, _compute, _read, record_pass, tmp_path, monkeypatch
    ):
        enable_precommit_reuse(monkeypatch, tmp_path)
        self.gate.project_root = tmp_path
        mock_run.return_value = MagicMock(returncode=1, stdout="failed", stderr="")

        result = self.gate.check_precommit()

        assert result.status == CheckStatus.FAILED
        record_pass.assert_not_called()

    def test_check_secrets_clean(self, tmp_path):
        """Test checking for secrets when none exist"""
        # Create temporary test files
        test_file = tmp_path / "test.py"
        test_file.write_text("# This is a test file\nprint('hello')")

        self.gate.project_root = tmp_path
        result = self.gate.check_secrets()
        assert result.status == CheckStatus.PASSED
        assert "No secrets detected" in result.message

    def test_check_secrets_found(self, tmp_path):
        """Test checking for secrets when they exist"""
        # Create temporary test file with secret
        test_file = tmp_path / "config.py"
        test_file.write_text("API_KEY = 'secret123456'")

        self.gate.project_root = tmp_path
        result = self.gate.check_secrets()
        assert result.status == CheckStatus.FAILED
        assert "Potential secrets detected" in result.message

    def test_check_secrets_ignore_config_optional(self, tmp_path, monkeypatch):
        """Without a gate-check.yaml, ignore list is empty and check still runs."""
        test_file = tmp_path / "config.py"
        test_file.write_text("API_KEY = 'secret123456'")

        work_repo = tmp_path / "work"
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work_repo))
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "org/proj")

        self.gate.project_root = tmp_path
        assert self.gate._load_secrets_ignore_patterns() == []
        result = self.gate.check_secrets()
        assert result.status == CheckStatus.FAILED

    def test_check_secrets_ignore_via_work_info(self, tmp_path, monkeypatch):
        """Files listed in work-repo gate-check.yaml are skipped."""
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "util.py").write_text('password = "guest"\n')
        settings_dir = tmp_path / "mainsite" / "settings"
        settings_dir.mkdir(parents=True)
        (settings_dir / "run_tests.py").write_text('GOOGLE_GEOLOC_API_KEY = "AIzatest"\n')

        work_repo = tmp_path / "work"
        info_dir = work_repo / "git.example.com" / "org/proj" / "info"
        info_dir.mkdir(parents=True)
        (info_dir / "gate-check.yaml").write_text(
            "secrets:\n"
            "  ignore:\n"
            "    - tests/util.py\n"
            "    - mainsite/settings/run_tests.py\n"
        )

        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work_repo))
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "org/proj")

        self.gate.project_root = tmp_path
        result = self.gate.check_secrets()
        assert result.status == CheckStatus.PASSED

    def test_check_secrets_ignore_supports_globs(self, tmp_path, monkeypatch):
        """Ignore patterns support fnmatch globs against the relative path."""
        settings_dir = tmp_path / "mainsite" / "settings"
        settings_dir.mkdir(parents=True)
        (settings_dir / "run_tests.py").write_text('API_KEY = "AIzatest"\n')

        work_repo = tmp_path / "work"
        info_dir = work_repo / "git.example.com" / "org/proj" / "info"
        info_dir.mkdir(parents=True)
        (info_dir / "gate-check.yaml").write_text(
            "secrets:\n"
            "  ignore:\n"
            "    - 'mainsite/settings/run_*.py'\n"
        )

        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work_repo))
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "org/proj")

        self.gate.project_root = tmp_path
        result = self.gate.check_secrets()
        assert result.status == CheckStatus.PASSED

    def test_check_secrets_ignore_is_not_read_from_the_gated_repo(
            self, tmp_path, monkeypatch):
        """`.lmer/gate-check.yaml` cannot silence the scan of its own repo.

        The repo-local config was introduced for the text-diff subset (#269)
        and every project is told to create one. An allowlist read from the
        tree under review is an allowlist the tree can write, so this key is
        the work repo's alone.
        """
        (tmp_path / "config.py").write_text("API_KEY = 'secret123456'")
        (tmp_path / ".lmer").mkdir()
        (tmp_path / ".lmer" / "gate-check.yaml").write_text(
            "secrets:\n  ignore:\n    - '**/*'\n")

        work_repo = tmp_path / "work"
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work_repo))
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "org/proj")

        self.gate.project_root = tmp_path
        assert self.gate._load_secrets_ignore_patterns() == []
        result = self.gate.check_secrets()
        assert result.status == CheckStatus.FAILED

    def test_check_secrets_ignore_as_a_string_is_ignored(self, tmp_path,
                                                         monkeypatch):
        """A bare string is not a one-entry list — iterating it yields `*`.

        `for p in "*.py"` gives the characters, `*` among them, and a lone
        `*` matches every relative path: one unquoted scalar would have
        silenced the whole scan.
        """
        (tmp_path / "config.py").write_text("API_KEY = 'secret123456'")

        work_repo = tmp_path / "work"
        info_dir = work_repo / "git.example.com" / "org/proj" / "info"
        info_dir.mkdir(parents=True)
        (info_dir / "gate-check.yaml").write_text(
            "secrets:\n  ignore: '*.py'\n")

        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work_repo))
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "org/proj")

        self.gate.project_root = tmp_path
        assert self.gate._load_secrets_ignore_patterns() == []
        result = self.gate.check_secrets()
        assert result.status == CheckStatus.FAILED

    def test_check_secrets_ignore_malformed_yaml_falls_back(self, tmp_path, monkeypatch):
        """A malformed gate-check.yaml does not crash the check."""
        test_file = tmp_path / "config.py"
        test_file.write_text("API_KEY = 'secret123456'")

        work_repo = tmp_path / "work"
        info_dir = work_repo / "git.example.com" / "org/proj" / "info"
        info_dir.mkdir(parents=True)
        (info_dir / "gate-check.yaml").write_text("secrets: [::: not valid yaml")

        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work_repo))
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "org/proj")

        self.gate.project_root = tmp_path
        assert self.gate._load_secrets_ignore_patterns() == []
        result = self.gate.check_secrets()
        assert result.status == CheckStatus.FAILED

    @staticmethod
    def _git(tmp_path, *args):
        """Run a git command in tmp_path, raising on failure."""
        subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_check_secrets_tracked_file_flagged(self, tmp_path):
        """In a git repo, a secret in a git-tracked file is flagged."""
        self._git(tmp_path, "init")
        secret_file = tmp_path / "config.py"
        secret_file.write_text("API_KEY = 'secret123456'")
        self._git(tmp_path, "add", "config.py")

        self.gate.project_root = tmp_path
        result = self.gate.check_secrets()
        assert result.status == CheckStatus.FAILED
        assert any("config.py" in line for line in result.details or [])

    def test_check_secrets_untracked_file_skipped(self, tmp_path):
        """In a git repo, a secret in an untracked file is NOT scanned.

        Untracked files cannot be committed without first being added (at which
        point they would be scanned), so the secret scan deliberately ignores
        them to avoid false positives from vendored / scratch files.
        """
        self._git(tmp_path, "init")
        # An untracked, never-added file containing a secret.
        (tmp_path / "scratch.py").write_text("API_KEY = 'secret123456'")
        # A tracked, clean file so git ls-files succeeds and returns >0 entries.
        clean = tmp_path / "main.py"
        clean.write_text("print('hello')\n")
        self._git(tmp_path, "add", "main.py")

        self.gate.project_root = tmp_path
        result = self.gate.check_secrets()
        assert result.status == CheckStatus.PASSED

    def test_check_secrets_gitignored_file_skipped(self, tmp_path):
        """In a git repo, a secret in a git-ignored file is NOT scanned."""
        self._git(tmp_path, "init")
        (tmp_path / ".gitignore").write_text("ignored.py\n")
        (tmp_path / "ignored.py").write_text("API_KEY = 'secret123456'")
        self._git(tmp_path, "add", ".gitignore")

        self.gate.project_root = tmp_path
        result = self.gate.check_secrets()
        assert result.status == CheckStatus.PASSED

    def test_check_secrets_tracked_non_ascii_filename_flagged(self, tmp_path):
        """A secret in a tracked file with a non-ASCII name is still flagged.

        Regression guard for the `git ls-files -z` fix: plain `git ls-files`
        quotes such paths (e.g. "caf\\303\\251.py"), which would not match the
        raw path from os.walk and would cause the file to be silently skipped.
        """
        self._git(tmp_path, "init")
        secret_file = tmp_path / "café.py"
        secret_file.write_text("API_KEY = 'secret123456'")
        self._git(tmp_path, "-c", "core.quotepath=true", "add", "café.py")

        self.gate.project_root = tmp_path
        result = self.gate.check_secrets()
        assert result.status == CheckStatus.FAILED
        assert any("café.py" in line for line in result.details or [])

    def test_check_code_quality_clean(self, tmp_path):
        """Test checking code quality when clean"""
        # Create temporary test file
        test_file = tmp_path / "main.py"
        test_file.write_text("def main():\n    return 42")

        self.gate.project_root = tmp_path
        result = self.gate.check_code_quality()
        assert result.status == CheckStatus.PASSED
        assert "No quality issues" in result.message

    def test_check_code_quality_issues(self, tmp_path):
        """Test checking code quality with issues"""
        # Create temporary test file with print statement
        test_file = tmp_path / "main.py"
        test_file.write_text("def main():\n    print('debug')\n    return 42")

        self.gate.project_root = tmp_path
        result = self.gate.check_code_quality()
        assert result.status == CheckStatus.WARNING
        assert "Quality issues found" in result.message

    def test_check_documentation_present(self):
        """Test checking documentation when present"""
        result = self.gate.check_documentation()
        # This should pass in the actual project
        assert result.status in [CheckStatus.PASSED, CheckStatus.WARNING, CheckStatus.FAILED]

    def test_check_documentation_missing(self, tmp_path):
        """Test checking documentation when missing"""
        self.gate.project_root = tmp_path
        result = self.gate.check_documentation()
        assert result.status == CheckStatus.FAILED
        assert "Critical documentation missing" in result.message

    def test_check_permissions_correct(self, tmp_path):
        """Test checking permissions when correct"""
        # Create temporary test file with correct permissions
        test_file = tmp_path / "test.py"
        test_file.write_text("# test")
        test_file.chmod(0o644)

        self.gate.project_root = tmp_path
        result = self.gate.check_permissions()
        assert result.status == CheckStatus.PASSED
        assert "Permissions correct" in result.message

    def test_check_permissions_world_writable(self, tmp_path):
        """Test checking permissions with world-writable file"""
        # Create temporary test file with wrong permissions
        test_file = tmp_path / "test.py"
        test_file.write_text("# test")
        test_file.chmod(0o666)

        self.gate.project_root = tmp_path
        result = self.gate.check_permissions()
        assert result.status == CheckStatus.FAILED
        assert "Permission issues found" in result.message

    def test_print_results_all_pass(self, capsys):
        """Test printing results when all pass"""
        self.gate.results = [
            CheckResult("Test 1", CheckStatus.PASSED, "All good"),
            CheckResult("Test 2", CheckStatus.PASSED, "Also good"),
        ]

        success = self.gate.print_results()
        assert success == True

        captured = capsys.readouterr()
        assert "ALL CHECKS PASSED" in captured.out

    def test_print_results_with_failures(self, capsys):
        """Test printing results with failures"""
        self.gate.results = [
            CheckResult("Test 1", CheckStatus.PASSED, "All good"),
            CheckResult("Test 2", CheckStatus.FAILED, "Bad", is_critical=True),
        ]

        success = self.gate.print_results()
        assert success == False

        captured = capsys.readouterr()
        assert "GATE BLOCKED" in captured.out

    def test_print_results_with_warnings(self, capsys):
        """Test printing results with warnings only"""
        self.gate.results = [
            CheckResult("Test 1", CheckStatus.PASSED, "All good"),
            CheckResult("Test 2", CheckStatus.WARNING, "Minor issue", is_critical=False),
        ]

        success = self.gate.print_results()
        assert success == True

        captured = capsys.readouterr()
        assert "All critical checks passed" in captured.out
        assert "1 warning(s) found" in captured.out

    def _mock_git(self, remotes=None, branch="feature-x", pushurls=None):
        """side_effect for subprocess.run covering run_push_gate's git calls.

        `remotes` maps remote name -> fetch URL; `pushurls` maps remote name
        -> `remote.<name>.pushurl`, which is what `get-url --push` reports
        and what `git push` actually dials (falling back to the fetch URL
        when unset). Unknown remotes fail like `git remote get-url` does.
        """
        remotes = remotes if remotes is not None else {"origin": "git@github.com:user/other-repo.git"}
        pushurls = pushurls or {}

        def fake_run(command, **kwargs):
            if command[:3] == ["git", "remote", "get-url"]:
                remote = command[-1]
                url = remotes.get(remote)
                if url is None:
                    return MagicMock(returncode=2, stdout="", stderr=f"error: No such remote '{remote}'")
                if "--push" in command:
                    url = pushurls.get(remote, url)
                # A remote may carry several pushurls; `get-url --push
                # --all` prints one per line and `git push` dials them all.
                if isinstance(url, (list, tuple)):
                    url = "\n".join(url)
                return MagicMock(returncode=0, stdout=url + "\n", stderr="")
            if command[:3] == ["git", "branch", "--show-current"]:
                return MagicMock(returncode=0, stdout=branch + "\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        return fake_run

    @patch.dict(os.environ, {"LMER_PUSH_ALLOW_LIST": ""})
    @patch('subprocess.run')
    def test_run_push_gate_not_allowed(self, mock_run):
        """Test push gate with repository not in allow list"""
        mock_run.side_effect = self._mock_git()

        success = self.gate.run_push_gate()
        assert success == False

    # NB: SSH remote URLs spell the repo `github.com:user/other-repo`, which
    # the grammar parses into host `github.com` + path `user/other-repo`; a
    # host-less entry names the path (#107).
    @patch.dict(os.environ, {"LMER_PUSH_ALLOW_LIST": "user/other-repo"})
    @patch('subprocess.run')
    def test_run_push_gate_allowed(self, mock_run):
        """Bare allow-list entry authorizes a branch push (default ref)."""
        mock_run.side_effect = self._mock_git()
        self.gate.run_commit_gate = MagicMock(return_value=True)

        success = self.gate.run_push_gate()
        assert success == True
        self.gate.run_commit_gate.assert_called_once()

    @patch.dict(os.environ, {"LMER_PUSH_ALLOW_LIST": "user/other-repo"})
    @patch('subprocess.run')
    def test_run_push_gate_bare_entry_refuses_tag(self, mock_run, capsys):
        """Bare entries are branch-only: no allow list silently gains tag rights."""
        mock_run.side_effect = self._mock_git()
        self.gate.run_commit_gate = MagicMock(return_value=True)

        success = self.gate.run_push_gate(ref="refs/tags/v0.2.0")
        assert success == False
        self.gate.run_commit_gate.assert_not_called()
        out = capsys.readouterr().out
        assert "Push not allowed" in out
        assert "refs/tags/v0.2.0" in out
        assert "Get explicit permission before pushing." in out

    @patch.dict(os.environ, {"LMER_PUSH_ALLOW_LIST": "user/other-repo|refs/tags/*"})
    @patch('subprocess.run')
    def test_run_push_gate_explicit_tag_entry_allows_tag(self, mock_run):
        """`repo|refs/tags/*` grants tag pushes to that repo."""
        mock_run.side_effect = self._mock_git()
        self.gate.run_commit_gate = MagicMock(return_value=True)

        success = self.gate.run_push_gate(ref="refs/tags/v0.2.0")
        assert success == True

    @patch.dict(os.environ, {"LMER_PUSH_ALLOW_LIST": "github.com/group/project|refs/heads/main"})
    @patch('subprocess.run')
    def test_run_push_gate_mirror_entry_scoped_to_remote(self, mock_run):
        """A mirror-repo entry authorizes only the remote it names."""
        remotes = {
            "origin": "git@gitlab.example.com:group/project.git",
            "mirror": "https://github.com/group/project.git",
        }
        mock_run.side_effect = self._mock_git(remotes=remotes)
        self.gate.run_commit_gate = MagicMock(return_value=True)

        assert self.gate.run_push_gate(ref="refs/heads/main", remote="mirror") == True
        assert self.gate.run_push_gate(ref="refs/heads/main", remote="origin") == False

    @patch.dict(os.environ, {
        "LMER_PUSH_ALLOW_LIST": "|refs/tags/*, user/other-repo|, a|b|c",
    })
    @patch('subprocess.run')
    def test_run_push_gate_malformed_entries_ignored(self, mock_run):
        """Malformed entries (empty half, extra delimiter) never fail open."""
        mock_run.side_effect = self._mock_git()
        self.gate.run_commit_gate = MagicMock(return_value=True)

        assert self.gate.run_push_gate(ref="refs/tags/v0.2.0") == False
        assert self.gate.run_push_gate(ref="refs/heads/main") == False
        self.gate.run_commit_gate.assert_not_called()

    @patch.dict(os.environ, {"LMER_PUSH_ALLOW_LIST": "user/other-repo"})
    @patch('subprocess.run')
    def test_run_push_gate_unresolvable_remote_fails_closed(self, mock_run, capsys):
        """A named remote git cannot resolve refuses — the allow list must
        never be skipped just because the remote lookup failed."""
        mock_run.side_effect = self._mock_git(remotes={})
        self.gate.run_commit_gate = MagicMock(return_value=True)

        assert self.gate.run_push_gate(remote="nosuch") == False
        self.gate.run_commit_gate.assert_not_called()
        assert "fail closed" in capsys.readouterr().out

    @patch.dict(os.environ, {"LMER_PUSH_ALLOW_LIST": "github.com/group/project|refs/heads/main"})
    @patch('subprocess.run')
    def test_run_push_gate_push_by_url_gated_on_the_url(self, mock_run):
        """`--remote <raw URL>` is gated against the URL itself: a matching
        grant authorizes it, anything else refuses — never a skip."""
        mock_run.side_effect = self._mock_git(remotes={})
        self.gate.run_commit_gate = MagicMock(return_value=True)

        assert self.gate.run_push_gate(
            ref="refs/heads/main",
            remote="https://github.com/group/project.git") == True
        assert self.gate.run_push_gate(
            ref="refs/heads/main",
            remote="https://github.com/attacker/repo.git") == False

    @patch.dict(os.environ, {"LMER_PUSH_ALLOW_LIST": "user/other-repo"})
    @patch('subprocess.run')
    def test_run_push_gate_refspec_authorizes_the_dst_side(self, mock_run, capsys):
        """`<src>:<dst>` authorization keys on dst: a branch-only grant must
        not authorize a refspec that lands on a remote TAG (fnmatch's `*`
        crosses `:`, so matching the whole refspec fails open here)."""
        mock_run.side_effect = self._mock_git()
        self.gate.run_commit_gate = MagicMock(return_value=True)

        assert self.gate.run_push_gate(
            ref="refs/heads/main:refs/tags/v9.9") == False
        self.gate.run_commit_gate.assert_not_called()
        # A branch-to-branch refspec inside the grant still works.
        assert self.gate.run_push_gate(
            ref="refs/heads/main:refs/heads/main") == True

    @patch.dict(os.environ, {"LMER_PUSH_ALLOW_LIST": "user/other-repo|refs/tags/*"})
    @patch('subprocess.run')
    def test_run_push_gate_refspec_dst_matches_tag_grant(self, mock_run):
        """The dst side is what a tag grant authorizes."""
        mock_run.side_effect = self._mock_git()
        self.gate.run_commit_gate = MagicMock(return_value=True)

        assert self.gate.run_push_gate(
            ref="refs/heads/main:refs/tags/v0.2.0") == True

    @patch.dict(os.environ, {
        "LMER_PUSH_ALLOW_LIST": "user/other-repo, user/other-repo|refs/tags/*",
    })
    @patch('subprocess.run')
    def test_run_push_gate_rejects_delete_refspecs(self, mock_run, capsys):
        """An empty `<src>` is a DELETION refspec (`git push origin
        :refs/tags/v0.5.0`). It is never authorized — deletion is at least
        as destructive as the force push already refused, and the release
        flow declares published tags immutable."""
        mock_run.side_effect = self._mock_git()
        self.gate.run_commit_gate = MagicMock(return_value=True)

        assert self.gate.run_push_gate(ref=":refs/heads/main") == False
        assert self.gate.run_push_gate(ref=":refs/tags/v0.5.0") == False
        assert self.gate.run_push_gate(ref=" :refs/tags/v0.5.0") == False
        self.gate.run_commit_gate.assert_not_called()
        assert "deletion" in capsys.readouterr().out

    @patch.dict(os.environ,
                {"LMER_PUSH_ALLOW_LIST": "forge.example.com/agents/global"})
    @patch('subprocess.run')
    def test_run_push_gate_push_by_url_match_is_anchored(self, mock_run):
        """The push-by-URL branch matches the allow-list entry against the
        URL's parsed identity, not as a substring: `--remote` is
        agent-supplied, so an unanchored rule would let any host embedding
        the allowed path authorize itself. The entry must pin the HOST."""
        mock_run.side_effect = self._mock_git(remotes={})
        self.gate.run_commit_gate = MagicMock(return_value=True)

        def push(url):
            return self.gate.run_push_gate(ref="refs/heads/main", remote=url)

        assert push("https://forge.example.com/agents/global.git") == True
        assert push("git@forge.example.com:agents/global.git") == True
        # The finding: an allowed path embedded in a foreign host's path.
        assert push("https://evil.example.com/mirror/agents/global.git") == False
        # Userinfo may only precede the first `/`: an attacker-chosen host
        # carrying the allowed identity in its PATH must not normalize to
        # that identity (git dials evil.example.com / evil.invalid here).
        assert push("https://evil.example.com/x@forge.example.com/agents/global") == False
        assert push("git@evil.invalid:x@forge.example.com/agents/global.git") == False
        assert push("https://evil.example.com/#@forge.example.com/agents/global") == False
        # A partial component, and a URL naming no repository at all.
        assert push("https://forge.example.com/agents/global-staging.git") == False
        assert push("/srv/mirrors/agents/global") == False

    @patch.dict(os.environ, {"LMER_PUSH_ALLOW_LIST": "agents/global"})
    @patch('subprocess.run')
    def test_run_push_gate_path_only_entry_does_not_authorize_by_url(self, mock_run):
        """A path-only entry authorizes nothing on the push-by-URL branch.

        Any forge can serve `agents/global`, so matching a bare path against
        an agent-supplied URL is the substring hole with a different prefix.
        Path-only grants remain valid for CONFIGURED remotes — see
        test_run_push_gate_named_remote_keeps_substring_match."""
        mock_run.side_effect = self._mock_git(remotes={})
        self.gate.run_commit_gate = MagicMock(return_value=True)

        def push(url):
            return self.gate.run_push_gate(ref="refs/heads/main", remote=url)

        assert push("https://forge.example.com/agents/global.git") == False
        assert push("https://evil.example.com/agents/global.git") == False

    @patch.dict(os.environ,
                {"LMER_PUSH_ALLOW_LIST": "github.com/user/other-repo|refs/tags/*"})
    @patch('subprocess.run')
    def test_run_push_gate_authorizes_the_pushurl_not_the_fetch_url(self, mock_run):
        """`git remote get-url` returns the FETCH url; `git push` uses
        `remote.<name>.pushurl` when configured. Authorizing the fetch url
        would green-light a push that lands somewhere else entirely."""
        mock_run.side_effect = self._mock_git(
            remotes={"origin": "https://github.com/user/other-repo.git"},
            pushurls={"origin": "https://evil.example.invalid/x.git"})
        self.gate.run_commit_gate = MagicMock(return_value=True)

        assert self.gate.run_push_gate(
            ref="refs/tags/v0.5.0", remote="origin") == False

    @patch.dict(os.environ, {"LMER_PUSH_ALLOW_LIST": "user/other-repo"})
    @patch('subprocess.run')
    def test_run_push_gate_refuses_detached_head(self, mock_run, capsys):
        """`git branch --show-current` exits 0 with EMPTY stdout on a
        detached HEAD. "refs/heads/" would then match "refs/heads/*" under
        fnmatch (`*` matches empty), so a bare entry would authorize a ref
        naming no branch. Refuse, like any other unresolvable ref."""
        mock_run.side_effect = self._mock_git(branch="")
        self.gate.run_commit_gate = MagicMock(return_value=True)

        assert self.gate.run_push_gate() == False
        self.gate.run_commit_gate.assert_not_called()
        assert "detached HEAD" in capsys.readouterr().out

    @patch.dict(os.environ, {"LMER_PUSH_ALLOW_LIST": "mirror/agents/global"})
    @patch('subprocess.run')
    def test_run_push_gate_named_remote_keeps_substring_match(self, mock_run):
        """Anchoring is scoped to push-by-URL. A configured remote's URL is
        operator-supplied, so the historical substring rule stays — deployed
        allow lists naming a path fragment keep working."""
        mock_run.side_effect = self._mock_git(
            remotes={"origin": "https://evil.example.com/mirror/agents/global.git"})
        self.gate.run_commit_gate = MagicMock(return_value=True)

        assert self.gate.run_push_gate(ref="refs/heads/main") == True

    def test_normalize_remote_url(self):
        """host/path for every URL form git accepts; None when the string
        names no repository (local path, bare host, path-less URL)."""
        cases = {
            "https://github.com/group/project.git": "github.com/group/project",
            "https://github.com/group/project": "github.com/group/project",
            "git@gitlab.example.com:group/project.git": "gitlab.example.com/group/project",
            "ssh://git@gitlab.example.com:2222/group/project.git": "gitlab.example.com/group/project",
            "https://token:x@forge.example.com/group/project.git": "forge.example.com/group/project",
            "https://GitHub.com/Group/Project.git": "github.com/group/project",
            "host/group/sub/project": "host/group/sub/project",
            "/srv/mirrors/group/project": None,
            "https://github.com/": None,
            "github.com": None,
            "": None,
        }
        for url, expected in cases.items():
            assert self.gate._normalize_remote_url(url) == expected, url

    @patch.dict(os.environ, {
        "LMER_PUSH_ALLOW_LIST": "user/other-repo, user/other-repo|refs/tags/*",
    })
    @patch('subprocess.run')
    def test_run_push_gate_rejects_force_short_and_glob_refs(self, mock_run, capsys):
        """Force pushes (+), short (ambiguous) refs, and glob refspecs are
        never authorized, however wide the grants: git resolves a short
        `v1.2.3` to refs/tags/v1.2.3 when such a tag exists, so a
        refs/heads/ normalization here would authorize the wrong ref class."""
        mock_run.side_effect = self._mock_git()
        self.gate.run_commit_gate = MagicMock(return_value=True)

        assert self.gate.run_push_gate(ref="+refs/heads/main") == False
        assert self.gate.run_push_gate(ref="main") == False
        assert self.gate.run_push_gate(ref="v1.2.3") == False
        assert self.gate.run_push_gate(ref="refs/heads/*") == False
        assert self.gate.run_push_gate(ref="refs/heads/main:") == False
        self.gate.run_commit_gate.assert_not_called()
        assert "Refusing to authorize ref" in capsys.readouterr().out

    def test_parse_push_allow_entry(self):
        """Entry grammar: bare = branch-only, `|` splits repo from ref pattern."""
        assert self.gate._parse_push_allow_entry("host/repo") == ("host/repo", "refs/heads/*")
        assert self.gate._parse_push_allow_entry("host/repo|refs/tags/*") == ("host/repo", "refs/tags/*")
        # Malformed entries parse to None (ignored by run_push_gate).
        assert self.gate._parse_push_allow_entry("|refs/tags/*") is None
        assert self.gate._parse_push_allow_entry("host/repo|") is None
        assert self.gate._parse_push_allow_entry("a|b|c") is None

    def test_run_commit_gate_skip_tests_omits_check_tests(self, capsys, tmp_path, monkeypatch):
        """skip_tests=True must not invoke check_tests, but still runs other checks."""
        # Redirect the log so the test doesn't clobber the real /tmp/gate-check.log.
        monkeypatch.setattr("lmer_cli.gates.GATE_CHECK_LOG_PATH", tmp_path / "gate-check.log")
        passed = CheckResult(name="x", status=CheckStatus.PASSED, message="ok")
        for attr in (
            "check_git_status", "check_staged_files", "check_branch",
            "check_precommit", "check_secrets", "check_code_quality",
            "check_documentation", "check_changelog",
            "check_deliverable_formats", "check_permissions",
        ):
            setattr(self.gate, attr, MagicMock(__name__=attr, return_value=passed))
        self.gate.check_tests = MagicMock(__name__="check_tests", return_value=passed)

        self.gate.run_commit_gate(skip_tests=True)

        self.gate.check_tests.assert_not_called()
        self.gate.check_precommit.assert_called_once()
        self.gate.check_secrets.assert_called_once()
        out = capsys.readouterr().out
        assert "Skipping Python Tests" in out
        # Library message stays neutral — env-var hint belongs to gate-commit.
        assert "LMER_QUICK_GATE_COMMIT" not in out

    def test_run_commit_gate_default_runs_tests(self, tmp_path, monkeypatch):
        """Without skip_tests, check_tests still runs (regression guard)."""
        # Redirect the log so the test doesn't clobber the real /tmp/gate-check.log.
        monkeypatch.setattr("lmer_cli.gates.GATE_CHECK_LOG_PATH", tmp_path / "gate-check.log")
        passed = CheckResult(name="x", status=CheckStatus.PASSED, message="ok")
        for attr in (
            "check_git_status", "check_staged_files", "check_branch",
            "check_tests", "check_precommit", "check_secrets",
            "check_code_quality", "check_documentation", "check_changelog",
            "check_deliverable_formats", "check_permissions",
        ):
            setattr(self.gate, attr, MagicMock(__name__=attr, return_value=passed))

        self.gate.run_commit_gate()

        self.gate.check_tests.assert_called_once()


class TestReceiptSummary:
    """GateSystem.receipt_summary(): best-effort, never fabricated (#88)."""

    def setup_method(self):
        self.gate = GateSystem(verbose=False)

    def test_failed_check_names_win(self):
        self.gate.results = [
            CheckResult("Python Tests", CheckStatus.FAILED),
            CheckResult("Branch Check", CheckStatus.PASSED),
            CheckResult("Security Check", CheckStatus.FAILED),
        ]
        assert self.gate.receipt_summary() == "failed: Python Tests, Security Check"

    def test_noncritical_failures_and_warnings_do_not_count(self):
        self.gate.results = [
            CheckResult("Code Quality", CheckStatus.FAILED, is_critical=False),
            CheckResult("Changelog", CheckStatus.WARNING),
        ]
        assert self.gate.receipt_summary() is None

    def test_pytest_tail_line_on_pass(self):
        self.gate.results = [
            CheckResult(
                "Python Tests", CheckStatus.PASSED,
                message="All 1397 tests passed",
                full_output="....\nlots of dots\n1397 passed in 41.80s\n",
            ),
        ]
        assert self.gate.receipt_summary() == "1397 passed in 41.80s"

    def test_decorated_pytest_summary_is_stripped(self):
        self.gate.results = [
            CheckResult(
                "Python Tests", CheckStatus.PASSED,
                full_output="=========== 5 passed, 1 skipped in 2.10s ===========\n",
            ),
        ]
        assert self.gate.receipt_summary() == "5 passed, 1 skipped in 2.10s"

    def test_none_without_parseable_output(self):
        self.gate.results = [
            CheckResult("Python Tests", CheckStatus.PASSED,
                        message="No tests directory found (skipped)"),
            CheckResult("Branch Check", CheckStatus.PASSED),
        ]
        assert self.gate.receipt_summary() is None

    def test_no_tests_ran_is_not_a_summary(self):
        # "no tests ran in 0.01s" carries no count — matching it would let a
        # receipt look like a test-suite pass when nothing executed.
        self.gate.results = [
            CheckResult("Python Tests", CheckStatus.PASSED,
                        full_output="no tests ran in 0.01s\n"),
        ]
        assert self.gate.receipt_summary() is None

    def test_empty_results_is_none(self):
        assert self.gate.receipt_summary() is None


class TestReceiptTestFields:
    """GateSystem.receipt_test_fields(): what the tests check covered (#269).

    The gate bins hand these to emit_gate_event as structured fields because
    `outcome` is "pass" with exit code 0 for a full suite, a narrowed subset
    and a reused pass alike. Unit-level here (results assembled by hand);
    the wiring from a real check_tests() run is covered by
    TestTextDiffFastPath and TestTestResultCache.
    """

    def setup_method(self):
        self.gate = GateSystem(verbose=False)

    def _tests(self, **kwargs):
        return CheckResult("Python Tests", CheckStatus.PASSED, **kwargs)

    def test_a_full_run_is_named_not_implied(self):
        self.gate.results = [self._tests(scope_targets=["tests/"])]
        assert self.gate.receipt_test_fields() == {
            "test_scope": "full suite", "test_targets": ["tests/"],
            "test_cache_verdict": "unknown",
            "test_cache_reason": "cache decision unavailable",
        }

    def test_a_narrowed_run_carries_its_scope_and_targets(self):
        self.gate.results = [self._tests(
            scope="text-diff subset",
            scope_targets=["tests/test_alpha.py", "tests/test_beta.py"],
        )]
        assert self.gate.receipt_test_fields() == {
            "test_scope": "text-diff subset",
            "test_targets": ["tests/test_alpha.py", "tests/test_beta.py"],
            "test_cache_verdict": "unknown",
            "test_cache_reason": "cache decision unavailable",
        }

    def test_a_cached_run_is_distinguishable_from_a_fresh_full_one(self):
        """The whole point: same outcome, same exit code, different receipt."""
        self.gate.results = [self._tests(scope="cached full suite",
                                         scope_targets=["tests/"])]
        cached = self.gate.receipt_test_fields()

        self.gate.results = [self._tests(scope_targets=["tests/"])]
        fresh = self.gate.receipt_test_fields()

        assert cached != fresh
        assert cached["test_scope"] == "cached full suite"
        assert fresh["test_scope"] == "full suite"

    def test_a_failing_run_still_reports_what_it_covered(self):
        """A subset failure is a fact about a subset — say which."""
        self.gate.results = [CheckResult(
            "Python Tests", CheckStatus.FAILED,
            scope="text-diff subset", scope_targets=["tests/test_alpha.py"],
        )]
        assert self.gate.receipt_test_fields() == {
            "test_scope": "text-diff subset",
            "test_targets": ["tests/test_alpha.py"],
            "test_cache_verdict": "unknown",
            "test_cache_reason": "cache decision unavailable",
        }

    def test_cache_verdict_and_reason_are_structured(self):
        self.gate.results = [self._tests(
            scope_targets=["tests/"],
            cache_verdict="miss",
            cache_reason="environment differs (PYTEST_ADDOPTS)",
        )]

        assert self.gate.receipt_test_fields() == {
            "test_scope": "full suite",
            "test_targets": ["tests/"],
            "test_cache_verdict": "miss",
            "test_cache_reason": "environment differs (PYTEST_ADDOPTS)",
        }

    def test_no_targets_means_no_claim(self):
        """A custom runner or a missing tests/ dir: the gate cannot say."""
        self.gate.results = [
            self._tests(message="No tests directory found (skipped)"),
            CheckResult("Branch Check", CheckStatus.PASSED),
        ]
        assert self.gate.receipt_test_fields() == {}

    def test_no_tests_check_at_all_is_no_claim(self):
        """LMER_QUICK_GATE_COMMIT (and a bypass) leave nothing to report."""
        self.gate.results = [CheckResult("Branch Check", CheckStatus.PASSED)]
        assert self.gate.receipt_test_fields() == {}
        self.gate.results = []
        assert self.gate.receipt_test_fields() == {}

    def test_the_returned_targets_are_a_copy(self):
        """A receipt field must not be an alias into the check's result."""
        targets = ["tests/test_alpha.py"]
        self.gate.results = [self._tests(scope="text-diff subset",
                                         scope_targets=targets)]
        fields = self.gate.receipt_test_fields()
        fields["test_targets"].append("tests/test_beta.py")
        assert targets == ["tests/test_alpha.py"]


class TestWriteLogFile:
    """Test the gate-check log file written for post-failure investigation."""

    def setup_method(self):
        self.gate = GateSystem(verbose=False)

    def test_writes_status_message_and_details(self, tmp_path):
        """Every check's status, message, and details land in the log."""
        self.gate.results = [
            CheckResult(
                name="Python Tests",
                status=CheckStatus.FAILED,
                message="Tests failed",
                details=["FAILED tests/test_x.py::test_y"],
            ),
        ]
        log_path = tmp_path / "gate-check.log"
        returned = self.gate.write_log_file(path=log_path)

        assert returned == log_path
        content = log_path.read_text()
        assert "[FAILED] Python Tests" in content
        assert "Message: Tests failed" in content
        assert "FAILED tests/test_x.py::test_y" in content

    def test_includes_full_output(self, tmp_path):
        """The untruncated full_output is what makes re-running unnecessary."""
        long_output = "\n".join(f"line {i}" for i in range(100))
        self.gate.results = [
            CheckResult(
                name="Python Tests",
                status=CheckStatus.FAILED,
                message="Tests failed",
                details=["line 99"],  # terminal only saw the tail
                full_output=long_output,
            ),
        ]
        log_path = tmp_path / "gate-check.log"
        self.gate.write_log_file(path=log_path)

        content = log_path.read_text()
        # The full body is present, not just the tail that reached the terminal.
        assert "line 0" in content
        assert "line 50" in content
        assert "line 99" in content
        assert "Full output:" in content

    def test_summary_counts_failures_and_warnings(self, tmp_path):
        """The summary line reflects critical failures vs. warnings."""
        self.gate.results = [
            CheckResult(name="A", status=CheckStatus.FAILED, message="boom"),
            CheckResult(name="B", status=CheckStatus.WARNING, message="meh"),
            CheckResult(
                name="C", status=CheckStatus.FAILED, message="non-crit",
                is_critical=False,
            ),
            CheckResult(name="D", status=CheckStatus.PASSED, message="ok"),
        ]
        log_path = tmp_path / "gate-check.log"
        self.gate.write_log_file(path=log_path)

        content = log_path.read_text()
        # Only the critical FAILED counts as a failure; non-critical is excluded.
        assert "SUMMARY: 1 critical failure(s), 1 warning(s)" in content
        assert "(non-critical)" in content

    def test_empty_full_output_renders_no_output_placeholder(self, tmp_path):
        """A check that ran but produced no output gets a "(no output)" marker."""
        self.gate.results = [
            CheckResult(
                name="Python Tests",
                status=CheckStatus.PASSED,
                message="ok",
                full_output="",
            ),
        ]
        log_path = tmp_path / "gate-check.log"
        self.gate.write_log_file(path=log_path)

        content = log_path.read_text()
        # full_output is non-None (the check shelled out) but empty -> placeholder.
        assert "Full output:" in content
        assert "(no output)" in content

    def test_none_full_output_omits_section(self, tmp_path):
        """A check with no captured output (full_output=None) has no output section."""
        self.gate.results = [
            CheckResult(
                name="Git Status",
                status=CheckStatus.PASSED,
                message="clean",
            ),
        ]
        log_path = tmp_path / "gate-check.log"
        self.gate.write_log_file(path=log_path)

        content = log_path.read_text()
        # Checks that never shell out (full_output stays None) get no output block.
        assert "Full output:" not in content

    def test_returns_none_on_write_error(self, tmp_path):
        """A log-write failure must never break gate-check itself."""
        self.gate.results = [
            CheckResult(name="A", status=CheckStatus.PASSED, message="ok"),
        ]
        # Point at a path whose parent does not exist and cannot be created.
        bad_path = tmp_path / "nope" / "gate-check.log"
        result = self.gate.write_log_file(path=bad_path)
        assert result is None

    def test_run_commit_gate_writes_log_and_prints_path(self, tmp_path, capsys, monkeypatch):
        """run_commit_gate persists the log and announces its path."""
        log_path = tmp_path / "gate-check.log"
        monkeypatch.setattr("lmer_cli.gates.GATE_CHECK_LOG_PATH", log_path)

        passed = CheckResult(name="x", status=CheckStatus.PASSED, message="ok")
        for attr in (
            "check_git_status", "check_staged_files", "check_branch",
            "check_tests", "check_precommit", "check_secrets",
            "check_code_quality", "check_documentation", "check_changelog",
            "check_deliverable_formats", "check_permissions",
        ):
            setattr(self.gate, attr, MagicMock(__name__=attr, return_value=passed))

        self.gate.run_commit_gate()

        assert log_path.exists()
        out = capsys.readouterr().out
        assert str(log_path) in out
        assert "Full check log written to" in out


class TestCommitGateOrderAndFailFast:
    """Cheapest-first check order and the suite fail-fast (#269)."""

    # Everything that runs in seconds; the suite is the only slow check and
    # is asserted separately as the last one.
    CHEAP_CHECKS = (
        "check_git_status", "check_staged_files", "check_branch",
        "check_secrets", "check_permissions", "check_deliverable_formats",
        "check_changelog", "check_documentation", "check_code_quality",
        "check_precommit",
    )

    def setup_method(self):
        self.gate = GateSystem(verbose=True)

    def _stub_checks(self, tmp_path, monkeypatch, outcomes=None):
        """Replace every check with a recorder; returns the call-order list."""
        # Redirect the log so the test doesn't clobber the real /tmp/gate-check.log.
        monkeypatch.setattr("lmer_cli.gates.GATE_CHECK_LOG_PATH",
                            tmp_path / "gate-check.log")
        outcomes = outcomes or {}
        calls = []

        def recorder(attr):
            outcome = outcomes.get(
                attr, CheckResult(name=attr, status=CheckStatus.PASSED))

            def run():
                calls.append(attr)
                return outcome

            run.__name__ = attr
            return run

        for attr in self.CHEAP_CHECKS + ("check_tests",):
            setattr(self.gate, attr, recorder(attr))
        return calls

    def test_tests_run_last_after_the_cheap_checks(self, tmp_path, monkeypatch):
        """The ~10-minute suite is only ever reached with everything else green."""
        calls = self._stub_checks(tmp_path, monkeypatch)

        self.gate.run_commit_gate()

        assert calls[-1] == "check_tests"
        # pre-commit (~36s) is the most expensive of the cheap ones, so it
        # sits directly in front of the suite.
        assert calls[-2] == "check_precommit"
        assert set(calls[:-1]) == set(self.CHEAP_CHECKS)

    def test_critical_failure_skips_the_suite(self, tmp_path, monkeypatch):
        """A blocked gate never spends ten minutes confirming it is blocked."""
        calls = self._stub_checks(tmp_path, monkeypatch, outcomes={
            "check_secrets": CheckResult(name="Security Check",
                                         status=CheckStatus.FAILED,
                                         message="Potential secrets detected"),
        })

        passed = self.gate.run_commit_gate()

        assert "check_tests" not in calls
        assert passed is False

    def test_skipped_suite_is_recorded_as_a_result(self, tmp_path, monkeypatch):
        """The receipt and the log read self.results, not the terminal."""
        self._stub_checks(tmp_path, monkeypatch, outcomes={
            "check_secrets": CheckResult(name="Security Check",
                                         status=CheckStatus.FAILED),
        })

        self.gate.run_commit_gate()

        skipped = [r for r in self.gate.results if r.name == "Python Tests"]
        assert len(skipped) == 1
        assert skipped[0].status == CheckStatus.SKIPPED
        assert "Security Check" in skipped[0].message
        # A skipped suite must never look like a passing one in the receipt.
        assert self.gate.receipt_summary() == "failed: Security Check"

    def test_skip_notice_names_every_blocking_check(self, tmp_path, monkeypatch,
                                                    capsys):
        """"Fix those first" is only actionable if it says which."""
        self._stub_checks(tmp_path, monkeypatch, outcomes={
            "check_secrets": CheckResult(name="Security Check",
                                         status=CheckStatus.FAILED),
            "check_precommit": CheckResult(name="Pre-commit Hooks",
                                           status=CheckStatus.FAILED),
        })

        self.gate.run_commit_gate()

        out = capsys.readouterr().out
        assert "Skipping Python Tests — 2 earlier check(s) failed" in out
        assert "(Security Check, Pre-commit Hooks)" in out
        assert "Fix those first; the suite has not been run." in out

    def test_warnings_do_not_skip_the_suite(self, tmp_path, monkeypatch):
        """Warnings don't block the gate, so they must not block the suite."""
        calls = self._stub_checks(tmp_path, monkeypatch, outcomes={
            "check_changelog": CheckResult(name="Changelog",
                                           status=CheckStatus.WARNING,
                                           is_critical=False),
            "check_branch": CheckResult(name="Branch Check",
                                        status=CheckStatus.WARNING,
                                        is_critical=False),
        })

        passed = self.gate.run_commit_gate()

        assert "check_tests" in calls
        assert passed is True

    def test_non_critical_failure_does_not_skip_the_suite(self, tmp_path,
                                                          monkeypatch):
        """A non-critical FAILED (e.g. Code Quality) leaves the gate passable."""
        calls = self._stub_checks(tmp_path, monkeypatch, outcomes={
            "check_code_quality": CheckResult(name="Code Quality",
                                              status=CheckStatus.FAILED,
                                              is_critical=False),
        })

        passed = self.gate.run_commit_gate()

        assert "check_tests" in calls
        assert passed is True

    def test_every_cheap_check_still_runs_after_an_early_failure(
            self, tmp_path, monkeypatch):
        """One pass must surface every cheap problem, not one per pass."""
        calls = self._stub_checks(tmp_path, monkeypatch, outcomes={
            "check_git_status": CheckResult(name="Git Status",
                                            status=CheckStatus.FAILED,
                                            message="Unstaged changes"),
        })

        self.gate.run_commit_gate()

        assert set(calls) == set(self.CHEAP_CHECKS)

    def test_skipped_suite_shows_in_the_log_file(self, tmp_path, monkeypatch):
        """Reading the log must distinguish "suite green" from "suite not run"."""
        log_path = tmp_path / "gate-check.log"
        self._stub_checks(tmp_path, monkeypatch, outcomes={
            "check_secrets": CheckResult(name="Security Check",
                                         status=CheckStatus.FAILED),
        })

        self.gate.run_commit_gate()

        content = log_path.read_text()
        assert "[SKIPPED] Python Tests" in content
        assert "Security Check" in content

    def test_skip_tests_flag_reaches_the_suite_check_unchanged(
            self, tmp_path, monkeypatch, capsys):
        """skip_tests=True keeps its own loud path, distinct from fail-fast."""
        calls = self._stub_checks(tmp_path, monkeypatch)

        self.gate.run_commit_gate(skip_tests=True)

        assert "check_tests" not in calls
        out = capsys.readouterr().out
        assert "⚠️  Skipping Python Tests" in out
        assert "earlier check(s) failed" not in out


class TestTextDiffFastPath:
    """A prose-only change runs the declared text-reading subset (#269).

    Nothing is skipped that the change could break — this repo's own tests
    read docs, so the honest "safe to skip" list is empty — the win is not
    asking the other ~8,600 tests.
    """

    SUBSET = ["tests/test_alpha.py", "tests/test_beta.py"]
    TEXT_DIFF = ["docs/CONTAINER.md", "README.md",
                 "changelog.d/20260813-x.yaml"]

    @pytest.fixture(autouse=True)
    def _clean_lmer_env(self, monkeypatch):
        # The config loader falls back to the work repo, which the running
        # session's env points at: without this the operational repo's
        # gate-check.yaml would decide these assertions.
        strip_lmer_env(monkeypatch)

    def setup_method(self):
        self.gate = GateSystem(verbose=True)
        self.pytest_calls = []

    def _repo(self, tmp_path, declared=None, subset_exists=True):
        """A project root with a tests/ dir and (optionally) a declaration."""
        (tmp_path / "tests").mkdir()
        if subset_exists:
            for path in self.SUBSET:
                (tmp_path / path).write_text("def test_x(): pass\n")
        if declared is not None:
            config_dir = tmp_path / ".lmer"
            config_dir.mkdir()
            (config_dir / "gate-check.yaml").write_text(
                "tests:\n  text_diff_subset:\n"
                + "".join(f"    - {p}\n" for p in declared)
            )
        self.gate.project_root = tmp_path
        return tmp_path

    def _dispatch(self, staged=None, unstaged="", untracked="",
                  pushed=None, range_resolves=True, pytest_stdout="5 passed in 0.5s"):
        """subprocess.run stand-in for the git probes and the pytest run."""
        def side_effect(cmd, *args, **kwargs):
            joined = " ".join(str(c) for c in cmd)
            if "-c" in cmd and any(str(a).startswith("import ") for a in cmd):
                return MagicMock(returncode=0, stdout="", stderr="")
            if "-m" in cmd and "pytest" in cmd:
                self.pytest_calls.append(list(cmd))
                return MagicMock(returncode=0, stdout=pytest_stdout, stderr="")
            if "rev-parse" in joined:
                return MagicMock(returncode=0 if range_resolves else 128,
                                 stdout="", stderr="")
            if "ls-files" in joined:
                return MagicMock(returncode=0, stdout=untracked, stderr="")
            if "--cached" in joined:
                return MagicMock(returncode=0,
                                 stdout="\0".join(staged or []), stderr="")
            if ".." in joined:
                return MagicMock(returncode=0,
                                 stdout="\0".join(pushed or []), stderr="")
            if "diff" in joined:
                return MagicMock(returncode=0, stdout=unstaged, stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        return side_effect

    def _ran(self):
        """The pytest path arguments of the single pytest invocation."""
        assert len(self.pytest_calls) == 1
        argv = self.pytest_calls[0]
        return [a for a in argv[argv.index("pytest") + 1:]
                if not a.startswith("-")]

    @patch('subprocess.run')
    def test_text_only_staged_diff_runs_the_subset(self, mock_run, tmp_path):
        self._repo(tmp_path, declared=self.SUBSET)
        mock_run.side_effect = self._dispatch(staged=self.TEXT_DIFF)

        result = self.gate.check_tests()

        assert self._ran() == self.SUBSET
        assert result.status == CheckStatus.PASSED

    @patch('subprocess.run')
    def test_one_python_file_in_the_diff_runs_the_full_suite(self, mock_run,
                                                             tmp_path):
        self._repo(tmp_path, declared=self.SUBSET)
        mock_run.side_effect = self._dispatch(
            staged=self.TEXT_DIFF + ["src/lmer_cli/gates.py"])

        self.gate.check_tests()

        assert self._ran() == ["tests/"]

    @patch('subprocess.run')
    def test_no_declaration_runs_the_full_suite(self, mock_run, tmp_path):
        """The default in every project that has not declared a subset."""
        self._repo(tmp_path, declared=None)
        mock_run.side_effect = self._dispatch(staged=self.TEXT_DIFF)

        self.gate.check_tests()

        assert self._ran() == ["tests/"]

    @patch('subprocess.run')
    def test_an_empty_declaration_runs_the_full_suite(self, mock_run, tmp_path):
        self._repo(tmp_path, declared=[])
        mock_run.side_effect = self._dispatch(staged=self.TEXT_DIFF)

        self.gate.check_tests()

        assert self._ran() == ["tests/"]

    @patch('subprocess.run')
    def test_unstaged_changes_run_the_full_suite(self, mock_run, tmp_path):
        """Never classify a tree the classifier has not looked at."""
        self._repo(tmp_path, declared=self.SUBSET)
        mock_run.side_effect = self._dispatch(staged=self.TEXT_DIFF,
                                              unstaged="src/lmer_cli/gates.py\n")

        self.gate.check_tests()

        assert self._ran() == ["tests/"]

    @patch('subprocess.run')
    def test_untracked_files_run_the_full_suite(self, mock_run, tmp_path):
        self._repo(tmp_path, declared=self.SUBSET)
        mock_run.side_effect = self._dispatch(staged=self.TEXT_DIFF,
                                              untracked="scratch.py\n")

        self.gate.check_tests()

        assert self._ran() == ["tests/"]

    @patch('subprocess.run')
    def test_nothing_staged_runs_the_full_suite(self, mock_run, tmp_path):
        """An empty change list is unknown, not "nothing to test"."""
        self._repo(tmp_path, declared=self.SUBSET)
        mock_run.side_effect = self._dispatch(staged=[])

        self.gate.check_tests()

        assert self._ran() == ["tests/"]

    @patch('subprocess.run')
    def test_push_gate_classifies_the_pushed_commits(self, mock_run, tmp_path):
        self._repo(tmp_path, declared=self.SUBSET)
        self.gate.in_push_gate = True
        self.gate.push_diff_base = "origin/feat/docs"
        mock_run.side_effect = self._dispatch(pushed=self.TEXT_DIFF,
                                              staged=["src/lmer_cli/gates.py"])

        self.gate.check_tests()

        assert self._ran() == self.SUBSET

    @patch('subprocess.run')
    def test_push_gate_with_an_unresolvable_range_runs_the_full_suite(
            self, mock_run, tmp_path):
        """A first push has no remote-tracking ref to diff against."""
        self._repo(tmp_path, declared=self.SUBSET)
        self.gate.in_push_gate = True
        self.gate.push_diff_base = "origin/feat/new"
        mock_run.side_effect = self._dispatch(pushed=self.TEXT_DIFF,
                                              range_resolves=False)

        self.gate.check_tests()

        assert self._ran() == ["tests/"]

    @patch('subprocess.run')
    def test_push_gate_without_a_base_runs_the_full_suite(self, mock_run,
                                                          tmp_path):
        """A tag push names no branch, so there is no range at all."""
        self._repo(tmp_path, declared=self.SUBSET)
        self.gate.in_push_gate = True
        mock_run.side_effect = self._dispatch(pushed=self.TEXT_DIFF)

        self.gate.check_tests()

        assert self._ran() == ["tests/"]

    @patch('subprocess.run')
    def test_kill_switch_runs_the_full_suite(self, mock_run, tmp_path,
                                             monkeypatch):
        self._repo(tmp_path, declared=self.SUBSET)
        monkeypatch.setenv("LMER_GATE_NO_FASTPATH", "1")
        mock_run.side_effect = self._dispatch(staged=self.TEXT_DIFF)

        self.gate.check_tests()

        assert self._ran() == ["tests/"]

    @patch('subprocess.run')
    def test_a_declared_path_that_is_gone_runs_the_full_suite_loudly(
            self, mock_run, tmp_path, capsys):
        """A stale declaration must not surface as a pytest usage error."""
        self._repo(tmp_path, declared=self.SUBSET, subset_exists=False)
        mock_run.side_effect = self._dispatch(staged=self.TEXT_DIFF)

        self.gate.check_tests()

        assert self._ran() == ["tests/"]
        out = capsys.readouterr().out
        assert "Ignoring the text-diff subset" in out
        assert "tests/test_alpha.py" in out

    @patch('subprocess.run')
    def test_the_notice_names_the_paths_the_subset_and_the_source(
            self, mock_run, tmp_path, capsys):
        """A fast path nobody can read is a waiver with better manners."""
        self._repo(tmp_path, declared=self.SUBSET)
        mock_run.side_effect = self._dispatch(staged=self.TEXT_DIFF)

        self.gate.check_tests()

        out = capsys.readouterr().out
        assert "Text-only diff — 3 changed path(s), all text" in out
        for path in self.TEXT_DIFF:
            assert path in out
        for path in self.SUBSET:
            assert path in out
        assert ".lmer/gate-check.yaml → tests.text_diff_subset" in out

    @patch('subprocess.run')
    def test_the_result_and_the_receipt_say_a_subset_ran(self, mock_run,
                                                         tmp_path):
        """Full suite, subset and not-run have to be three distinct readings."""
        self._repo(tmp_path, declared=self.SUBSET)
        mock_run.side_effect = self._dispatch(staged=self.TEXT_DIFF)

        result = self.gate.check_tests()
        self.gate.results.append(result)

        assert result.scope == "text-diff subset"
        assert result.message == "text-diff subset: 5 tests passed"
        assert self.gate.receipt_summary() == "text-diff subset: 5 passed in 0.5s"
        assert self.gate.receipt_test_fields() == {
            "test_scope": "text-diff subset", "test_targets": list(self.SUBSET),
            "test_cache_verdict": "miss",
            "test_cache_reason": "fingerprint unavailable",
        }

    @patch('subprocess.run')
    def test_a_full_run_carries_no_scope(self, mock_run, tmp_path):
        self._repo(tmp_path, declared=None)
        mock_run.side_effect = self._dispatch(staged=self.TEXT_DIFF)

        result = self.gate.check_tests()
        self.gate.results.append(result)

        assert result.scope is None
        assert result.message == "All 5 tests passed"
        assert self.gate.receipt_summary() == "5 passed in 0.5s"
        # No scope to disclaim on the terminal, but the receipt still NAMES
        # the full run — absence there means "cannot say", not "everything".
        assert self.gate.receipt_test_fields() == {
            "test_scope": "full suite", "test_targets": ["tests/"],
            "test_cache_verdict": "miss",
            "test_cache_reason": "fingerprint unavailable",
        }

    @patch('subprocess.run')
    def test_a_failing_subset_run_names_its_scope(self, mock_run, tmp_path):
        self._repo(tmp_path, declared=self.SUBSET)

        def failing(cmd, *args, **kwargs):
            if "-m" in cmd and "pytest" in cmd:
                self.pytest_calls.append(list(cmd))
                return MagicMock(returncode=1,
                                 stdout="FAILED tests/test_alpha.py::test_x",
                                 stderr="")
            return self._dispatch(staged=self.TEXT_DIFF)(cmd, *args, **kwargs)

        mock_run.side_effect = failing

        result = self.gate.check_tests()

        assert result.status == CheckStatus.FAILED
        assert result.message == "Tests failed (text-diff subset)"
        assert result.details[0] == "ran: tests/test_alpha.py, tests/test_beta.py"

    @patch('subprocess.run')
    def test_a_custom_test_runner_still_wins(self, mock_run, tmp_path,
                                             monkeypatch):
        """The project's own runner owns the invocation; don't second-guess it."""
        self._repo(tmp_path, declared=self.SUBSET)
        work_repo = tmp_path / "work"
        info_dir = work_repo / "git.example.com" / "org/proj" / "info"
        info_dir.mkdir(parents=True)
        runner = info_dir / "gate-check-run-tests.sh"
        runner.write_text("#!/bin/sh\nexit 0\n")
        runner.chmod(0o755)
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work_repo))
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "org/proj")
        monkeypatch.setenv("LMER_TASK", "develop")
        mock_run.side_effect = self._dispatch(staged=self.TEXT_DIFF)

        result = self.gate.check_tests()
        self.gate.results.append(result)

        assert self.pytest_calls == []
        assert "Custom test runner" in result.message
        # The gate did not choose the targets, so it claims no scope: an
        # unknown coverage must not reach the receipt as "full suite".
        assert result.scope_targets is None
        assert self.gate.receipt_test_fields() == {}


class TestChangedPathsAgainstRealGit:
    """What the classifier is handed, measured against git itself (#269).

    Rename detection is a property of git's output, not of this code, so it
    is asked of a real repository rather than a mock.
    """

    SUBSET = ["tests/test_alpha.py"]

    @pytest.fixture(autouse=True)
    def _clean_lmer_env(self, monkeypatch):
        strip_lmer_env(monkeypatch)

    def setup_method(self):
        self.gate = GateSystem(verbose=True)

    @staticmethod
    def _git(cwd, *args):
        subprocess.run(["git", *args], cwd=cwd, check=True,
                       capture_output=True, text=True)

    def _repo(self, tmp_path, declared=False):
        """A repo whose HEAD holds a module under src/ and (maybe) a subset."""
        self._git(tmp_path, "init")
        self._git(tmp_path, "config", "user.email", "test@test.com")
        self._git(tmp_path, "config", "user.name", "Test")
        (tmp_path / "src").mkdir()
        (tmp_path / "docs").mkdir()
        (tmp_path / "src" / "code.py").write_text("def f():\n    return 1\n")
        if declared:
            (tmp_path / "tests").mkdir()
            for path in self.SUBSET:
                (tmp_path / path).write_text("def test_x(): pass\n")
            (tmp_path / ".lmer").mkdir()
            (tmp_path / ".lmer" / "gate-check.yaml").write_text(
                "tests:\n  text_diff_subset:\n"
                + "".join(f"    - {p}\n" for p in self.SUBSET))
        self._git(tmp_path, "add", "-A")
        self._git(tmp_path, "commit", "-m", "base")
        self.gate.project_root = tmp_path
        return tmp_path

    def test_a_rename_reports_both_sides(self, tmp_path):
        """`git mv src/code.py docs/notes.md` removes a module.

        With git's default rename detection `--name-only` prints the
        post-image alone, so the change reads as one markdown file and the
        module's disappearance is invisible to the classifier.
        """
        self._repo(tmp_path)
        self._git(tmp_path, "mv", "src/code.py", "docs/notes.md")

        assert sorted(self.gate._changed_paths()) == ["docs/notes.md",
                                                      "src/code.py"]

    def test_a_rename_out_of_code_runs_the_full_suite(self, tmp_path):
        """The reason the pre-image has to be visible."""
        self._repo(tmp_path, declared=True)
        self._git(tmp_path, "mv", "src/code.py", "docs/notes.md")

        assert self.gate._text_diff_fast_path() is None

    def test_a_prose_only_rename_still_takes_the_fast_path(self, tmp_path):
        """--no-renames widens what is SEEN, not what counts as code."""
        self._repo(tmp_path, declared=True)
        (tmp_path / "docs" / "old.md").write_text("# old\n")
        self._git(tmp_path, "add", "docs/old.md")
        self._git(tmp_path, "commit", "-m", "doc")
        self._git(tmp_path, "mv", "docs/old.md", "docs/new.md")

        fast_path = self.gate._text_diff_fast_path()

        assert fast_path is not None
        assert sorted(fast_path[0]) == ["docs/new.md", "docs/old.md"]


class TestTestResultCache:
    """A tree already proven green is not proven again (#269).

    The unit tests in tests/test_gate_cache.py own the key; these own the
    wiring — that a pass is recorded, that a hit skips the suite and says so,
    and above all that a narrowed run can never answer for a full one.
    """

    TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
    SUBSET = ["tests/test_alpha.py"]

    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path, monkeypatch):
        # The config loader falls back to the work repo the running session
        # points at, and the cache must never be the operational one.
        strip_lmer_env(monkeypatch)
        self.cache_dir = tmp_path / "cache"
        monkeypatch.setenv(gate_cache.CACHE_DIR_ENV, str(self.cache_dir))
        self.gate = GateSystem(verbose=True, commit_handoff=True)
        self.gate.project_root = tmp_path / "repo"
        (self.gate.project_root / "tests").mkdir(parents=True)
        self.pytest_calls = []

    def _declare_subset(self):
        """Give the project a text-diff subset, so MR A's fast path can fire."""
        for path in self.SUBSET:
            (self.gate.project_root / path).write_text("def test_x(): pass\n")
        config_dir = self.gate.project_root / ".lmer"
        config_dir.mkdir()
        (config_dir / "gate-check.yaml").write_text(
            "tests:\n  text_diff_subset:\n"
            + "".join(f"    - {p}\n" for p in self.SUBSET))

    def _dispatch(self, tree=None, status="", blobs=None, version="3.12.1",
                  rc=0, stdout="8 passed in 12.3s", staged=(),
                  status_after=None, sites=None, indexed_tree=None):
        """subprocess.run stand-in for the git probes, the interpreter and pytest.

        `status_after` swaps the porcelain output once pytest has run — the
        shape of an edit landing during a ten-minute suite. `sites` stands in
        for the interpreter's site directories, so a rebuilt image's changed
        dependencies can be played back here.
        """
        tree = self.TREE if tree is None else tree
        indexed_tree = tree if indexed_tree is None else indexed_tree
        blobs = blobs or {}
        state = {"status": status}

        def side_effect(cmd, *args, **kwargs):
            joined = " ".join(str(c) for c in cmd)
            if str(cmd[0]).endswith("gate-check-run-tests.sh"):
                return MagicMock(returncode=0, stdout="ok", stderr="")
            if "-c" in cmd:
                code = cmd[cmd.index("-c") + 1]
                if "sys.version" in code:
                    return MagicMock(returncode=0 if version else 1,
                                     stdout=version, stderr="")
                if "sysconfig" in code:
                    return MagicMock(returncode=0, stdout=json.dumps({
                        "prefix": str(self.gate.project_root),
                        "sites": list(sites or []),
                    }), stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")
            if "-m" in cmd and "pytest" in cmd:
                self.pytest_calls.append(list(cmd))
                if status_after is not None:
                    state["status"] = status_after
                return MagicMock(returncode=rc, stdout=stdout, stderr="")
            if "--show-toplevel" in joined:
                # Porcelain paths are relative to THIS, not to any cwd.
                return MagicMock(returncode=0 if tree else 128,
                                 stdout=str(self.gate.project_root), stderr="")
            if "rev-parse" in joined:
                return MagicMock(returncode=0 if tree else 128, stdout=tree,
                                 stderr="")
            if "write-tree" in joined:
                return MagicMock(returncode=0 if indexed_tree else 128,
                                 stdout=indexed_tree, stderr="")
            if "status" in joined:
                return MagicMock(returncode=0, stdout=state["status"], stderr="")
            if "hash-object" in joined:
                paths = cmd[cmd.index("--") + 1:]
                return MagicMock(
                    returncode=0,
                    stdout="".join(f"{blobs.get(p, 'b' * 40)}\n" for p in paths),
                    stderr="")
            if "ls-files" in joined:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "--cached" in joined:
                return MagicMock(returncode=0, stdout="\0".join(staged),
                                 stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        return side_effect

    def _ran(self):
        """The pytest path arguments of the LAST pytest invocation."""
        argv = self.pytest_calls[-1]
        return [a for a in argv[argv.index("pytest") + 1:]
                if not a.startswith("-")]

    def _entries(self):
        return sorted(self.cache_dir.glob("*.json")) \
            if self.cache_dir.exists() else []

    @patch('subprocess.run')
    def test_a_second_run_on_the_same_tree_reuses_the_pass(self, mock_run):
        mock_run.side_effect = self._dispatch()

        first = self.gate.check_tests()
        assert len(self.pytest_calls) == 1
        assert len(self._entries()) == 1

        second = self.gate.check_tests()

        assert len(self.pytest_calls) == 1, "the suite ran a second time"
        assert second.status == CheckStatus.PASSED
        assert first.scope is None and second.scope == "cached full suite"
        assert second.message == "cached full suite: 8 passed in 12.3s"

    @patch('subprocess.run')
    def test_a_non_commit_gate_never_probes_the_index(self, mock_run):
        self.gate.commit_handoff = False
        mock_run.side_effect = self._dispatch()

        self.gate.check_tests()
        self.gate.check_tests()

        commands = [" ".join(str(part) for part in call.args[0])
                    for call in mock_run.call_args_list]
        assert not any("git write-tree" in command for command in commands)

    @patch('subprocess.run')
    def test_a_disabled_cache_never_probes_the_index(self, mock_run,
                                                     monkeypatch):
        monkeypatch.setenv("LMER_GATE_NO_CACHE", "1")
        mock_run.side_effect = self._dispatch()

        self.gate.check_tests()

        commands = [" ".join(str(part) for part in call.args[0])
                    for call in mock_run.call_args_list]
        assert not any("git write-tree" in command for command in commands)

    @patch('subprocess.run')
    def test_a_failed_index_probe_does_not_discard_the_passing_cache_entry(
            self, mock_run):
        mock_run.side_effect = self._dispatch(indexed_tree="")

        result = self.gate.check_tests()

        assert result.status == CheckStatus.PASSED
        assert len(self._entries()) == 1
        assert self.gate.handoff_test_cache_after_commit() is False

    @patch('subprocess.run')
    def test_a_commit_handoff_makes_the_clean_post_commit_tree_a_hit(
            self, mock_run):
        post_tree = "a" * 40
        (self.gate.project_root / "module.py").write_text("value = 2\n")
        mock_run.side_effect = self._dispatch(
            status="M  module.py\0",
            blobs={"module.py": "b" * 40},
            indexed_tree=post_tree,
        )

        fresh = self.gate.check_tests()
        assert fresh.cache_verdict == "miss"
        assert len(self.pytest_calls) == 1

        mock_run.side_effect = self._dispatch(
            tree=post_tree, status="", indexed_tree=post_tree
        )
        assert self.gate.handoff_test_cache_after_commit() is True

        cached = self.gate.check_tests()
        assert len(self.pytest_calls) == 1, "the push-shaped lookup reran pytest"
        assert cached.cache_verdict == "hit"
        assert cached.scope == "cached full suite"

    @patch('subprocess.run')
    def test_handoff_refuses_a_commit_that_does_not_match_the_tested_index(
            self, mock_run):
        tested_tree = "a" * 40
        mock_run.side_effect = self._dispatch(indexed_tree=tested_tree)
        self.gate.check_tests()

        mock_run.side_effect = self._dispatch(tree="b" * 40, status="")

        assert self.gate.handoff_test_cache_after_commit() is False

    def test_handoff_failure_never_changes_the_commits_result(self, monkeypatch):
        self.gate._passing_suite = object()
        monkeypatch.setattr(
            gate_cache,
            "committed_tree",
            lambda _run: (_ for _ in ()).throw(OSError("cache unavailable")),
        )

        assert self.gate.handoff_test_cache_after_commit() is False

    @patch('subprocess.run')
    def test_the_receipt_distinguishes_a_cached_pass_from_a_fresh_one(
            self, mock_run):
        """The receipt is what a reviewer reads when asking "was this tested?".

        Both readings, since a summary is free text a consumer would have to
        parse: the prose line AND the structured fields the gate bins hand
        to emit_gate_event.
        """
        mock_run.side_effect = self._dispatch()

        fresh = self.gate.check_tests()
        self.gate.results.append(fresh)
        assert self.gate.receipt_test_fields() == {
            "test_scope": "full suite", "test_targets": ["tests/"],
            "test_cache_verdict": "miss",
            "test_cache_reason": "no current matching pass",
        }

        self.gate.results = [self.gate.check_tests()]

        assert self.gate.receipt_summary() == "cached full suite: 8 passed in 12.3s"
        assert self.gate.receipt_test_fields() == {
            "test_scope": "cached full suite", "test_targets": ["tests/"],
            "test_cache_verdict": "hit",
            "test_cache_reason": "exact fingerprint pass found",
        }

    def test_cache_miss_decisions_are_structured_at_the_source(self,
                                                               monkeypatch):
        fingerprint = MagicMock()

        monkeypatch.setattr(gate_cache, "cache_enabled", lambda: False)
        assert self.gate._cache_miss_decision(fingerprint) == (
            "disabled", f"disabled by {gate_cache.DISABLE_ENV}")

        monkeypatch.setattr(gate_cache, "cache_enabled", lambda: True)
        assert self.gate._cache_miss_decision(None) == (
            "miss", "fingerprint unavailable")

        monkeypatch.setattr(
            gate_cache, "environment_mismatch", lambda _fingerprint: ["TERM"]
        )
        assert self.gate._cache_miss_decision(fingerprint) == (
            "miss", "same tree and invocation, environment differs (TERM)")

        monkeypatch.setattr(
            gate_cache, "environment_mismatch", lambda _fingerprint: []
        )
        assert self.gate._cache_miss_decision(fingerprint) == (
            "miss", "no current matching pass")

    @patch('subprocess.run')
    def test_the_hit_says_when_it_was_proven_and_how_to_force_a_rerun(
            self, mock_run, capsys):
        mock_run.side_effect = self._dispatch()

        self.gate.check_tests()
        capsys.readouterr()
        result = self.gate.check_tests()

        out = capsys.readouterr().out
        assert "cached result for this exact tree" in out
        assert "Proven green" in out
        assert "8 passed in 12.3s" in out
        assert "working tree clean" in out
        assert "LMER_GATE_NO_CACHE=1 forces a re-run." in out
        # The log reader gets the same facts, and is told plainly that
        # nothing ran.
        assert "The suite did NOT run" in result.full_output

    @patch('subprocess.run')
    def test_a_modified_working_tree_file_misses(self, mock_run):
        mock_run.side_effect = self._dispatch()
        self.gate.check_tests()

        (self.gate.project_root / "module.py").write_text("value = 2\n")
        mock_run.side_effect = self._dispatch(
            status=" M module.py\0", blobs={"module.py": "c" * 40})
        self.gate.check_tests()

        assert len(self.pytest_calls) == 2

    @patch('subprocess.run')
    def test_a_staged_only_change_misses(self, mock_run):
        mock_run.side_effect = self._dispatch()
        self.gate.check_tests()

        (self.gate.project_root / "module.py").write_text("value = 2\n")
        mock_run.side_effect = self._dispatch(
            status="M  module.py\0", blobs={"module.py": "c" * 40})
        self.gate.check_tests()

        assert len(self.pytest_calls) == 2

    def _prose_change(self):
        """A staged prose-only change: MR A's fast path fires on this tree."""
        self._declare_subset()
        (self.gate.project_root / "README.md").write_text("hello\n")
        return self._dispatch(status="M  README.md\0", staged=["README.md"],
                              blobs={"README.md": "d" * 40})

    @patch('subprocess.run')
    def test_a_subset_pass_never_satisfies_a_full_suite_run(self, mock_run,
                                                            monkeypatch):
        """The sharpest failure mode: a partial pass laundered into a full one.

        MR A runs the declared text-reading subset for a prose-only change.
        That pass is real, but it is a pass of a handful of files — and a
        release push needs the whole suite. The invocation is in the key, so
        the subset entry cannot answer for it.
        """
        mock_run.side_effect = self._prose_change()

        self.gate.check_tests()
        assert self._ran() == self.SUBSET
        assert len(self._entries()) == 1

        monkeypatch.setenv("LMER_GATE_NO_FASTPATH", "1")
        self.gate.check_tests()

        assert len(self.pytest_calls) == 2, "the subset pass answered for the full suite"
        assert self._ran() == ["tests/"]
        assert len(self._entries()) == 2

    @patch('subprocess.run')
    def test_a_full_suite_pass_never_satisfies_a_subset_run(self, mock_run,
                                                            monkeypatch):
        """The same failure mode from the other side.

        Harmless in outcome — running the subset again costs seconds — but a
        cache that answered here would be one keyed on something other than
        the invocation, which is the property the case above rests on.
        """
        monkeypatch.setenv("LMER_GATE_NO_FASTPATH", "1")
        mock_run.side_effect = self._prose_change()

        self.gate.check_tests()
        assert self._ran() == ["tests/"]

        monkeypatch.delenv("LMER_GATE_NO_FASTPATH")
        self.gate.check_tests()

        assert len(self.pytest_calls) == 2
        assert self._ran() == self.SUBSET

    @patch('subprocess.run')
    def test_a_cached_subset_run_is_reported_as_both(self, mock_run):
        """"cached text-diff subset" — nothing ran, and what it was that had."""
        self._declare_subset()
        (self.gate.project_root / "README.md").write_text("hello\n")
        mock_run.side_effect = self._dispatch(
            status="M  README.md\0", staged=["README.md"],
            blobs={"README.md": "d" * 40}, stdout="2 passed in 0.2s")

        self.gate.check_tests()
        result = self.gate.check_tests()
        self.gate.results.append(result)

        assert len(self.pytest_calls) == 1
        assert result.scope == "cached text-diff subset"
        assert self.gate.receipt_summary() == \
            "cached text-diff subset: 2 passed in 0.2s"
        # The narrowed targets survive the cache hit: "which tests stood in
        # for the suite?" has to be answerable from the receipt alone.
        assert self.gate.receipt_test_fields() == {
            "test_scope": "cached text-diff subset",
            "test_targets": self.SUBSET,
            "test_cache_verdict": "hit",
            "test_cache_reason": "exact fingerprint pass found",
        }

    @patch('subprocess.run')
    def test_a_different_interpreter_version_misses(self, mock_run):
        mock_run.side_effect = self._dispatch(version="3.12.1")
        self.gate.check_tests()

        mock_run.side_effect = self._dispatch(version="3.13.0")
        self.gate.check_tests()

        assert len(self.pytest_calls) == 2

    @patch('subprocess.run')
    def test_a_different_import_path_misses(self, mock_run, monkeypatch):
        """PYTHONPATH decides what pytest imports, so it is part of the run."""
        monkeypatch.delenv("PYTHONPATH", raising=False)
        mock_run.side_effect = self._dispatch()
        self.gate.check_tests()

        monkeypatch.setenv("PYTHONPATH", "/somewhere/else/src")
        self.gate.check_tests()

        assert len(self.pytest_calls) == 2

    @patch('subprocess.run')
    def test_a_narrowing_pytest_addopts_misses(self, mock_run, monkeypatch):
        """pytest reads PYTEST_ADDOPTS itself, so a run under `-k something`
        is a different run than the argv alone describes — and handing it back
        under the full-suite key would defeat the subset separation from
        outside the argv."""
        monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
        mock_run.side_effect = self._dispatch()
        self.gate.check_tests()

        monkeypatch.setenv("PYTEST_ADDOPTS", "-k test_nothing_matches")
        self.gate.check_tests()

        assert len(self.pytest_calls) == 2
        # One file, not one per environment: the environment is checked out of
        # the entry, so the second run overwrites rather than accumulates.
        assert len(self._entries()) == 1

    @patch('subprocess.run')
    def test_an_environment_miss_names_the_variable_that_moved(
            self, mock_run, monkeypatch, capsys):
        """A miss on the environment alone is otherwise silent — ten minutes
        of suite and no way to tell why. Names only: the environment carries
        credentials."""
        monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
        mock_run.side_effect = self._dispatch()
        self.gate.check_tests()
        capsys.readouterr()

        monkeypatch.setenv("PYTEST_ADDOPTS", "-k test_nothing_matches")
        self.gate.check_tests()

        out = capsys.readouterr().out
        assert "Cache miss: same tree and invocation, environment differs" in out
        assert "PYTEST_ADDOPTS" in out
        assert "test_nothing_matches" not in out

    @patch('subprocess.run')
    def test_a_first_run_on_a_new_tree_says_nothing_about_the_environment(
            self, mock_run, capsys):
        """There is no entry to differ from, and a notice on every cold run
        would train the reader to skip the one that matters."""
        mock_run.side_effect = self._dispatch()

        self.gate.check_tests()

        assert "environment differs" not in capsys.readouterr().out

    @patch('subprocess.run')
    def test_an_ambient_variable_the_suite_reads_misses(self, mock_run,
                                                        monkeypatch):
        """This suite's own tests branch on ambient state (integration skips,
        GIT_CONFIG_* in the doctor tests), so the whole environment is keyed."""
        monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
        mock_run.side_effect = self._dispatch()
        self.gate.check_tests()

        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        self.gate.check_tests()

        assert len(self.pytest_calls) == 2

    @patch('subprocess.run')
    def test_a_volatile_variable_still_hits(self, mock_run, monkeypatch):
        """The other half: `_`/`SHLVL`/`OLDPWD`/`__MISE_SESSION` churn between
        invocations and cannot reach a test, so keying on them would mean
        never hitting. `__MISE_SESSION` is the one that did: `~/.bashrc`'s
        mise activation mints a fresh token per login shell, and a gate runs
        from a new shell every time."""
        monkeypatch.setenv("SHLVL", "1")
        monkeypatch.setenv("__MISE_SESSION", "0aBcD")
        mock_run.side_effect = self._dispatch()
        self.gate.check_tests()

        monkeypatch.setenv("SHLVL", "7")
        monkeypatch.setenv("_", "/usr/bin/whatever")
        monkeypatch.setenv("__MISE_SESSION", "9zYxW")
        self.gate.check_tests()

        assert len(self.pytest_calls) == 1, "the suite ran a second time"

    @patch('subprocess.run')
    def test_a_changed_dependency_surface_misses(self, mock_run, tmp_path):
        """An image rebuilt with other packages leaves the tree, the argv and
        the Python version all unchanged — the one drift nothing else sees."""
        site = tmp_path / "site-packages"
        (site / "pytest-9.1.1.dist-info").mkdir(parents=True)
        mock_run.side_effect = self._dispatch(sites=[str(site)])
        self.gate.check_tests()

        (site / "pytest-9.2.0.dist-info").mkdir()
        mock_run.side_effect = self._dispatch(sites=[str(site)])
        self.gate.check_tests()

        assert len(self.pytest_calls) == 2

    @patch('subprocess.run')
    def test_a_cache_directory_this_uid_does_not_own_is_a_miss(
            self, mock_run, monkeypatch):
        """A forged entry mints a passing gate, so a directory somebody else
        created in a world-writable /tmp is refused — as a miss, never as an
        error that breaks the gate."""
        mock_run.side_effect = self._dispatch()
        self.gate.check_tests()
        other = os.geteuid() + 1
        monkeypatch.setattr(os, "geteuid", lambda: other)

        result = self.gate.check_tests()

        assert result.status == CheckStatus.PASSED
        assert len(self.pytest_calls) == 2, "an entry in a foreign directory answered"

    @patch('subprocess.run')
    def test_a_failing_suite_is_never_recorded(self, mock_run):
        """Flaky failures would stick, and re-running a failure is cheap."""
        mock_run.side_effect = self._dispatch(
            rc=1, stdout="FAILED tests/test_x.py::test_y")

        self.gate.check_tests()

        assert self._entries() == []

    @patch('subprocess.run')
    def test_the_kill_switch_misses_and_records_nothing(self, mock_run,
                                                        monkeypatch):
        monkeypatch.setenv("LMER_GATE_NO_CACHE", "1")
        mock_run.side_effect = self._dispatch()

        self.gate.check_tests()
        self.gate.check_tests()

        assert len(self.pytest_calls) == 2
        assert self._entries() == []

    @patch('subprocess.run')
    def test_outside_a_git_repo_nothing_is_read_or_written(self, mock_run):
        """Unknown means run the suite, always."""
        mock_run.side_effect = self._dispatch(tree="")

        self.gate.check_tests()
        self.gate.check_tests()

        assert len(self.pytest_calls) == 2
        assert self._entries() == []

    @patch('subprocess.run')
    def test_a_status_probe_that_fails_records_nothing(self, mock_run):
        mock_run.side_effect = self._dispatch(status="not porcelain\0")

        self.gate.check_tests()

        assert len(self.pytest_calls) == 1
        assert self._entries() == []

    @patch('subprocess.run')
    def test_a_tree_that_changed_during_the_run_is_not_recorded(self, mock_run):
        """A ten-minute suite is long enough for an edit to land inside it;
        recording the pre-run tree would publish a verdict for code no run
        ever saw."""
        (self.gate.project_root / "module.py").write_text("value = 2\n")
        mock_run.side_effect = self._dispatch(
            status_after=" M module.py\0", blobs={"module.py": "c" * 40})

        result = self.gate.check_tests()

        assert result.status == CheckStatus.PASSED
        assert self._entries() == []

    @patch('subprocess.run')
    def test_a_custom_test_runner_is_never_cached(self, mock_run, tmp_path,
                                                  monkeypatch):
        """The project owns that invocation, and its inputs are not in the key."""
        info_dir = tmp_path / "work" / "git.example.com" / "org/proj" / "info"
        info_dir.mkdir(parents=True)
        runner = info_dir / "gate-check-run-tests.sh"
        runner.write_text("#!/bin/sh\nexit 0\n")
        runner.chmod(0o755)
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path / "work"))
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "org/proj")
        mock_run.side_effect = self._dispatch()

        self.gate.check_tests()
        result = self.gate.check_tests()

        assert "Custom test runner" in result.message
        assert self._entries() == []


class TestGateConfigSources:
    """Both gate-check.yaml locations, and one loader behind them (#269)."""

    @pytest.fixture(autouse=True)
    def _clean_lmer_env(self, monkeypatch):
        strip_lmer_env(monkeypatch)

    def setup_method(self):
        self.gate = GateSystem(verbose=True)

    @staticmethod
    def _work_info(tmp_path, monkeypatch, body):
        info_dir = tmp_path / "work" / "git.example.com" / "org/proj" / "info"
        info_dir.mkdir(parents=True)
        (info_dir / "gate-check.yaml").write_text(body)
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path / "work"))
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "org/proj")

    @staticmethod
    def _repo_config(tmp_path, body):
        (tmp_path / ".lmer").mkdir()
        (tmp_path / ".lmer" / "gate-check.yaml").write_text(body)

    def test_repo_local_declaration_is_preferred(self, tmp_path, monkeypatch):
        self.gate.project_root = tmp_path
        self._repo_config(tmp_path,
                          "tests:\n  text_diff_subset:\n    - tests/near.py\n")
        self._work_info(tmp_path, monkeypatch,
                        "tests:\n  text_diff_subset:\n    - tests/far.py\n")

        subset, source = self.gate._text_diff_subset()

        assert subset == ["tests/near.py"]
        assert source == tmp_path / ".lmer" / "gate-check.yaml"

    def test_work_repo_info_is_the_fallback(self, tmp_path, monkeypatch):
        self.gate.project_root = tmp_path
        self._work_info(tmp_path, monkeypatch,
                        "tests:\n  text_diff_subset:\n    - tests/far.py\n")

        subset, source = self.gate._text_diff_subset()

        assert subset == ["tests/far.py"]
        assert source.parent.name == "info"

    def test_agents_global_precommit_opt_in_is_found(self, tmp_path, monkeypatch):
        """Pin the external authorization path deployed for issue #318."""
        work = tmp_path / "work"
        info = work / "git.example.com" / "org" / "proj" / "info"
        info.mkdir(parents=True)
        config = info / "gate-check.yaml"
        config.write_text("precommit:\n  reuse_all_files: true\n")
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "org/proj")

        reuse, source = self.gate._gate_config_lookup(
            "precommit", "reuse_all_files", repo_local=False
        )

        assert reuse is True
        assert source == config

    def test_lookup_is_per_key_not_per_file(self, tmp_path, monkeypatch):
        """A repo declaring only `tests` must not hide the work repo's
        `secrets.ignore` — that list is the reason this file already existed."""
        self.gate.project_root = tmp_path
        self._repo_config(tmp_path,
                          "tests:\n  text_diff_subset:\n    - tests/near.py\n")
        self._work_info(tmp_path, monkeypatch,
                        "secrets:\n  ignore:\n    - vendor/keys.py\n")

        assert self.gate._load_secrets_ignore_patterns() == ["vendor/keys.py"]
        assert self.gate._text_diff_subset()[0] == ["tests/near.py"]

    def test_the_repo_local_source_is_scoped_to_the_test_subset(
            self, tmp_path, monkeypatch):
        """Only `tests.text_diff_subset` reads the gated repo's own config.

        `secrets.ignore` keeps its pre-#269 work-repo-only lookup: it
        silences a check, and the repo it would be read from is the repo the
        check is scanning.
        """
        self.gate.project_root = tmp_path
        self._repo_config(tmp_path,
                          "tests:\n  text_diff_subset:\n    - tests/near.py\n"
                          "secrets:\n  ignore:\n    - '**/*'\n")
        self._work_info(tmp_path, monkeypatch,
                        "secrets:\n  ignore:\n    - vendor/keys.py\n")

        assert self.gate._text_diff_subset()[0] == ["tests/near.py"]
        assert self.gate._load_secrets_ignore_patterns() == ["vendor/keys.py"]

    def test_unparseable_repo_config_is_not_fatal(self, tmp_path, monkeypatch):
        self.gate.project_root = tmp_path
        self._repo_config(tmp_path, "tests: [::: not valid yaml")

        assert self.gate._text_diff_subset() == ([], None)
        assert self.gate._load_secrets_ignore_patterns() == []

    def test_a_non_list_subset_is_ignored(self, tmp_path):
        self.gate.project_root = tmp_path
        self._repo_config(tmp_path, "tests:\n  text_diff_subset: tests/near.py\n")

        assert self.gate._text_diff_subset() == ([], None)

    def test_no_config_anywhere(self, tmp_path, monkeypatch):
        self.gate.project_root = tmp_path
        monkeypatch.delenv("LMER_WORK_REPO_PATH", raising=False)

        assert self.gate._gate_config_sources() == []
        assert self.gate._text_diff_subset() == ([], None)
        assert self.gate._load_secrets_ignore_patterns() == []


class TestPushGateSetsTheDiffBase:
    """run_push_gate hands the tests check the range it is about to push."""

    @pytest.fixture(autouse=True)
    def _clean_lmer_env(self, monkeypatch):
        strip_lmer_env(monkeypatch)

    def setup_method(self):
        self.gate = GateSystem(verbose=True)
        self.gate.run_commit_gate = MagicMock(return_value=True)

    @patch('subprocess.run')
    def test_branch_push_sets_the_remote_tracking_base(self, mock_run,
                                                       monkeypatch):
        monkeypatch.setenv("LMER_PUSH_ALLOW_LIST", "gitlab.example.com")
        mock_run.side_effect = [
            MagicMock(returncode=0,
                      stdout="https://gitlab.example.com/org/proj.git\n",
                      stderr=""),
            MagicMock(returncode=0, stdout="feat/docs\n", stderr=""),
        ]

        assert self.gate.run_push_gate() is True
        assert self.gate.in_push_gate is True
        assert self.gate.push_diff_base == "origin/feat/docs"

    @patch('subprocess.run')
    def test_tag_push_leaves_the_base_unset(self, mock_run, monkeypatch):
        monkeypatch.setenv("LMER_PUSH_ALLOW_LIST",
                           "gitlab.example.com|refs/tags/*")
        mock_run.return_value = MagicMock(
            returncode=0, stdout="https://gitlab.example.com/org/proj.git\n",
            stderr="")

        assert self.gate.run_push_gate(ref="refs/tags/v1.0.0") is True
        assert self.gate.in_push_gate is True
        assert self.gate.push_diff_base is None

    @patch('subprocess.run')
    def test_a_refspec_leaves_the_base_unset(self, mock_run, monkeypatch):
        """`--ref <src>:<dst>` pushes src, and src need not be HEAD.

        The base would be diffed against HEAD, so a refspec pushing another
        branch to main would have the CURRENT branch's diff decide how much
        of the suite runs. bin/gate-push hands the refspec to git verbatim.
        """
        monkeypatch.setenv("LMER_PUSH_ALLOW_LIST", "gitlab.example.com")
        mock_run.return_value = MagicMock(
            returncode=0, stdout="https://gitlab.example.com/org/proj.git\n",
            stderr="")

        assert self.gate.run_push_gate(
            ref="refs/heads/feat/code:refs/heads/main") is True
        assert self.gate.in_push_gate is True
        assert self.gate.push_diff_base is None
        # No base is what "run everything" is made of: the tests check asks
        # for the changed paths and is told they are unknown.
        assert self.gate._changed_paths() is None

    @patch('subprocess.run')
    def test_an_explicit_branch_ref_leaves_the_base_unset(self, mock_run,
                                                          monkeypatch):
        """Same hole without a colon: `--ref refs/heads/<other-branch>`."""
        monkeypatch.setenv("LMER_PUSH_ALLOW_LIST", "gitlab.example.com")
        mock_run.return_value = MagicMock(
            returncode=0, stdout="https://gitlab.example.com/org/proj.git\n",
            stderr="")

        assert self.gate.run_push_gate(ref="refs/heads/feat/other") is True
        assert self.gate.in_push_gate is True
        assert self.gate.push_diff_base is None
        assert self.gate._changed_paths() is None


class TestCheckChangelog:
    """Test the changelog check functionality"""

    def setup_method(self):
        self.gate = GateSystem(verbose=True)

    def test_no_changelog_file(self, tmp_path):
        """Test warning when no changelog file exists"""
        (tmp_path / "main.py").write_text("print('hello')")
        self.gate.project_root = tmp_path
        result = self.gate.check_changelog()
        assert result.status == CheckStatus.WARNING
        assert "No changelog file found" in result.message
        assert not result.is_critical

    def test_changelog_yaml_exists_not_staged(self, tmp_path, monkeypatch):
        """Test warning when CHANGELOG.yaml exists but is not staged"""
        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], check=True, capture_output=True)
        (tmp_path / "CHANGELOG.yaml").write_text("unreleased:\n  added: []\n")
        (tmp_path / "main.py").write_text("print('hello')")
        subprocess.run(["git", "add", "main.py"], check=True, capture_output=True)

        self.gate.project_root = tmp_path
        result = self.gate.check_changelog()
        assert result.status == CheckStatus.WARNING
        assert "Changelog not updated" in result.message
        assert not result.is_critical

    def test_changelog_yaml_staged(self, tmp_path, monkeypatch):
        """Test pass when CHANGELOG.yaml is staged"""
        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], check=True, capture_output=True)
        (tmp_path / "CHANGELOG.yaml").write_text("unreleased:\n  added: []\n")
        subprocess.run(["git", "add", "CHANGELOG.yaml"], check=True, capture_output=True)

        self.gate.project_root = tmp_path
        result = self.gate.check_changelog()
        assert result.status == CheckStatus.PASSED
        assert "Changelog updated" in result.message

    def test_changes_md_detected(self, tmp_path, monkeypatch):
        """Test that CHANGES.md is recognized as a changelog"""
        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], check=True, capture_output=True)
        (tmp_path / "CHANGES.md").write_text("# Changes\n")
        (tmp_path / "main.py").write_text("print('hello')")
        subprocess.run(["git", "add", "main.py"], check=True, capture_output=True)

        self.gate.project_root = tmp_path
        result = self.gate.check_changelog()
        assert result.status == CheckStatus.WARNING
        assert "CHANGES.md" in result.message

    def test_history_txt_detected(self, tmp_path, monkeypatch):
        """Test that HISTORY.txt is recognized as a changelog"""
        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], check=True, capture_output=True)
        (tmp_path / "HISTORY.txt").write_text("History\n")
        (tmp_path / "main.py").write_text("print('hello')")
        subprocess.run(["git", "add", "main.py"], check=True, capture_output=True)

        self.gate.project_root = tmp_path
        result = self.gate.check_changelog()
        assert result.status == CheckStatus.WARNING
        assert "HISTORY.txt" in result.message

    def test_changelog_md_staged(self, tmp_path, monkeypatch):
        """Test pass when CHANGELOG.md is staged"""
        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], check=True, capture_output=True)
        (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")
        subprocess.run(["git", "add", "CHANGELOG.md"], check=True, capture_output=True)

        self.gate.project_root = tmp_path
        result = self.gate.check_changelog()
        assert result.status == CheckStatus.PASSED
        assert "CHANGELOG.md" in result.message

    def test_case_insensitive_detection(self, tmp_path, monkeypatch):
        """Test that changelog detection is case-insensitive"""
        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], check=True, capture_output=True)
        (tmp_path / "Changelog.md").write_text("# Changelog\n")
        (tmp_path / "main.py").write_text("print('hello')")
        subprocess.run(["git", "add", "main.py"], check=True, capture_output=True)

        self.gate.project_root = tmp_path
        result = self.gate.check_changelog()
        assert result.status == CheckStatus.WARNING
        assert "Changelog.md" in result.message

    def test_multiple_changelogs_detected(self, tmp_path, monkeypatch):
        """Test that multiple changelog files are all detected"""
        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], check=True, capture_output=True)
        (tmp_path / "CHANGELOG.yaml").write_text("unreleased:\n  added: []\n")
        (tmp_path / "CHANGES.md").write_text("# Changes\n")
        (tmp_path / "main.py").write_text("print('hello')")
        subprocess.run(["git", "add", "main.py"], check=True, capture_output=True)

        self.gate.project_root = tmp_path
        result = self.gate.check_changelog()
        assert result.status == CheckStatus.WARNING
        assert "Changelog not updated" in result.message

    def test_fragment_staged_with_changelog(self, tmp_path, monkeypatch):
        """Test pass when a changelog.d fragment is staged (changelog present)"""
        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], check=True, capture_output=True)
        (tmp_path / "CHANGELOG.yaml").write_text("unreleased:\n  added: []\n")
        (tmp_path / "changelog.d").mkdir()
        (tmp_path / "changelog.d" / "20260718-foo.yaml").write_text("added:\n  - foo\n")
        subprocess.run(["git", "add", "changelog.d/20260718-foo.yaml"], check=True, capture_output=True)

        self.gate.project_root = tmp_path
        result = self.gate.check_changelog()
        assert result.status == CheckStatus.PASSED
        assert "changelog.d/20260718-foo.yaml" in result.message

    def test_fragment_staged_without_changelog_file(self, tmp_path, monkeypatch):
        """Test pass when a fragment is staged and no changelog file exists"""
        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], check=True, capture_output=True)
        (tmp_path / "changelog.d").mkdir()
        (tmp_path / "changelog.d" / "20260718-foo.yaml").write_text("added:\n  - foo\n")
        subprocess.run(["git", "add", "changelog.d/20260718-foo.yaml"], check=True, capture_output=True)

        self.gate.project_root = tmp_path
        result = self.gate.check_changelog()
        assert result.status == CheckStatus.PASSED
        assert "changelog.d/20260718-foo.yaml" in result.message

    def test_fragment_only_repo_nothing_staged(self, tmp_path, monkeypatch):
        """Fragment-only repo (changelog.d/, no changelog file): the
        no-changelog warning points at the fragment convention"""
        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], check=True, capture_output=True)
        (tmp_path / "changelog.d").mkdir()
        (tmp_path / "main.py").write_text("print('hello')")
        subprocess.run(["git", "add", "main.py"], check=True, capture_output=True)

        self.gate.project_root = tmp_path
        result = self.gate.check_changelog()
        assert result.status == CheckStatus.WARNING
        assert "No changelog file found" in result.message
        assert any("changelog.d/YYYYMMDD-<topic>.yaml" in d for d in result.details)
        assert not result.is_critical

    def test_changelog_d_present_nothing_staged(self, tmp_path, monkeypatch):
        """Test warning details point at fragment authoring when changelog.d exists"""
        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], check=True, capture_output=True)
        (tmp_path / "CHANGELOG.yaml").write_text("unreleased:\n  added: []\n")
        (tmp_path / "changelog.d").mkdir()
        (tmp_path / "main.py").write_text("print('hello')")
        subprocess.run(["git", "add", "main.py"], check=True, capture_output=True)

        self.gate.project_root = tmp_path
        result = self.gate.check_changelog()
        assert result.status == CheckStatus.WARNING
        assert "Changelog not updated" in result.message
        assert any("changelog.d/YYYYMMDD-<topic>.yaml" in d for d in result.details)
        assert not result.is_critical

    def test_no_changelog_d_warning_unchanged(self, tmp_path, monkeypatch):
        """Regression: without changelog.d/, warning behavior is unchanged"""
        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], check=True, capture_output=True)
        (tmp_path / "CHANGELOG.yaml").write_text("unreleased:\n  added: []\n")
        (tmp_path / "main.py").write_text("print('hello')")
        subprocess.run(["git", "add", "main.py"], check=True, capture_output=True)

        self.gate.project_root = tmp_path
        result = self.gate.check_changelog()
        assert result.status == CheckStatus.WARNING
        assert "Changelog not updated" in result.message
        assert result.details == ["Update the changelog if this commit includes user-facing changes"]

    def test_non_yaml_fragment_does_not_count(self, tmp_path, monkeypatch):
        """Test that a staged non-YAML file under changelog.d/ is not a fragment"""
        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], check=True, capture_output=True)
        (tmp_path / "CHANGELOG.yaml").write_text("unreleased:\n  added: []\n")
        (tmp_path / "changelog.d").mkdir()
        (tmp_path / "changelog.d" / "README.md").write_text("# Fragments\n")
        subprocess.run(["git", "add", "changelog.d/README.md"], check=True, capture_output=True)

        self.gate.project_root = tmp_path
        result = self.gate.check_changelog()
        assert result.status == CheckStatus.WARNING
        assert "Changelog not updated" in result.message

    def test_changelog_staged_with_changelog_d_present(self, tmp_path, monkeypatch):
        """Test pass when CHANGELOG.yaml itself is staged and changelog.d exists"""
        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], check=True, capture_output=True)
        (tmp_path / "CHANGELOG.yaml").write_text("unreleased:\n  added: []\n")
        (tmp_path / "changelog.d").mkdir()
        subprocess.run(["git", "add", "CHANGELOG.yaml"], check=True, capture_output=True)

        self.gate.project_root = tmp_path
        result = self.gate.check_changelog()
        assert result.status == CheckStatus.PASSED
        assert "Changelog updated" in result.message

    def test_staged_fragment_deletion_does_not_count(self, tmp_path, monkeypatch):
        """A staged DELETION of a fragment is not a changelog update"""
        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], check=True, capture_output=True)
        (tmp_path / "CHANGELOG.yaml").write_text("unreleased:\n  added: []\n")
        (tmp_path / "changelog.d").mkdir()
        frag = tmp_path / "changelog.d" / "20260718-old.yaml"
        frag.write_text("added:\n- x\n")
        subprocess.run(["git", "add", "changelog.d/20260718-old.yaml"], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "seed"], check=True, capture_output=True)
        subprocess.run(["git", "rm", "changelog.d/20260718-old.yaml"], check=True, capture_output=True)

        self.gate.project_root = tmp_path
        result = self.gate.check_changelog()
        assert result.status == CheckStatus.WARNING
        assert "Changelog not updated" in result.message

    def test_nested_fragment_does_not_count(self, tmp_path, monkeypatch):
        """Only files directly under changelog.d/ count as fragments"""
        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], check=True, capture_output=True)
        (tmp_path / "CHANGELOG.yaml").write_text("unreleased:\n  added: []\n")
        (tmp_path / "changelog.d" / "sub").mkdir(parents=True)
        (tmp_path / "changelog.d" / "sub" / "20260718-x.yaml").write_text("added:\n- x\n")
        subprocess.run(["git", "add", "changelog.d/sub/20260718-x.yaml"], check=True, capture_output=True)

        self.gate.project_root = tmp_path
        result = self.gate.check_changelog()
        assert result.status == CheckStatus.WARNING
        assert "Changelog not updated" in result.message

    def test_non_yaml_fragment_counts_in_non_ctl_repo(self, tmp_path, monkeypatch):
        """changelog.d beside a non-YAML changelog is another tool's
        convention — any staged fragment file counts, and the warning hint
        does not prescribe the ctl YAML format"""
        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], check=True, capture_output=True)
        (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")
        (tmp_path / "changelog.d").mkdir()
        (tmp_path / "changelog.d" / "123.feature.rst").write_text("Added a thing.\n")
        subprocess.run(["git", "add", "changelog.d/123.feature.rst"], check=True, capture_output=True)

        self.gate.project_root = tmp_path
        result = self.gate.check_changelog()
        assert result.status == CheckStatus.PASSED
        assert "123.feature.rst" in result.message

        # And with nothing staged, the hint is convention-neutral.
        subprocess.run(["git", "reset"], check=True, capture_output=True)
        (tmp_path / "main.py").write_text("print('hello')")
        subprocess.run(["git", "add", "main.py"], check=True, capture_output=True)
        result = self.gate.check_changelog()
        assert result.status == CheckStatus.WARNING
        assert any("this repo's fragment convention" in d for d in result.details)
        assert not any("YYYYMMDD-<topic>.yaml" in d for d in result.details)


class TestCheckDeliverableFormats:
    """check_deliverable_formats(): spec-class deliverables are Markdown (#102)."""

    def setup_method(self):
        self.gate = GateSystem(verbose=True)

    @patch('subprocess.run')
    def test_staged_spec_docx_warns(self, mock_run):
        """A staged spec.docx gets a warning naming the file and the md rule"""
        mock_run.return_value = MagicMock(returncode=0, stdout="docs/spec.docx", stderr="")

        result = self.gate.check_deliverable_formats()
        assert result.status == CheckStatus.WARNING
        assert not result.is_critical
        assert any("docs/spec.docx" in d for d in result.details)
        assert any("Markdown (.md)" in d for d in result.details)

    @patch('subprocess.run')
    def test_staged_spec_md_passes(self, mock_run):
        """A staged spec.md is the contract — no warning"""
        mock_run.return_value = MagicMock(returncode=0, stdout="docs/spec.md", stderr="")

        result = self.gate.check_deliverable_formats()
        assert result.status == CheckStatus.PASSED

    @patch('subprocess.run')
    def test_staged_report_pdf_warns(self, mock_run):
        """A staged report.pdf matches the spec-class naming and warns"""
        mock_run.return_value = MagicMock(returncode=0, stdout="reports/q2-report.pdf", stderr="")

        result = self.gate.check_deliverable_formats()
        assert result.status == CheckStatus.WARNING
        assert not result.is_critical
        assert any("reports/q2-report.pdf" in d for d in result.details)

    @patch('subprocess.run')
    def test_unrelated_pdf_passes(self, mock_run):
        """Binary files outside spec/plan/report naming are out of scope"""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="docs/vendor-manual.pdf\nfixtures/inspect.pdf",
            stderr=""
        )

        result = self.gate.check_deliverable_formats()
        assert result.status == CheckStatus.PASSED

    @patch('subprocess.run')
    def test_naming_matched_in_directory_component(self, mock_run):
        """A spec-named directory flags binary documents inside it"""
        mock_run.return_value = MagicMock(returncode=0, stdout="docs/specs/api.odt", stderr="")

        result = self.gate.check_deliverable_formats()
        assert result.status == CheckStatus.WARNING

    @patch('subprocess.run')
    def test_leading_word_boundary_only(self, mock_run):
        """'spec' inside a word (inspect) does not match; a leading match (specification) does"""
        mock_run.return_value = MagicMock(returncode=0, stdout="docs/inspection.docx", stderr="")
        result = self.gate.check_deliverable_formats()
        assert result.status == CheckStatus.PASSED

        mock_run.return_value = MagicMock(returncode=0, stdout="docs/specification.docx", stderr="")
        result = self.gate.check_deliverable_formats()
        assert result.status == CheckStatus.WARNING

    @patch('subprocess.run')
    def test_nothing_staged_passes(self, mock_run):
        """An empty index has nothing to flag"""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = self.gate.check_deliverable_formats()
        assert result.status == CheckStatus.PASSED

    @patch('subprocess.run')
    def test_git_failure_is_noncritical_warning(self, mock_run):
        """A git failure degrades to a non-critical warning, never a hard fail"""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")

        result = self.gate.check_deliverable_formats()
        assert result.status == CheckStatus.WARNING
        assert not result.is_critical


class TestGateCommands:
    """Test the gate command scripts"""

    def test_gate_check_exists(self):
        """Test that gate-check command exists and is executable"""
        script_path = Path(__file__).parent.parent / "bin" / "gate-check"
        assert script_path.exists()
        assert os.access(script_path, os.X_OK)

    def test_gate_commit_exists(self):
        """Test that gate-commit command exists and is executable"""
        script_path = Path(__file__).parent.parent / "bin" / "gate-commit"
        assert script_path.exists()
        assert os.access(script_path, os.X_OK)

    def test_gate_push_exists(self):
        """Test that gate-push command exists and is executable"""
        script_path = Path(__file__).parent.parent / "bin" / "gate-push"
        assert script_path.exists()
        assert os.access(script_path, os.X_OK)

    def test_gate_checkpoint_exists(self):
        """Test that gate-checkpoint command exists"""
        script_path = Path(__file__).parent.parent / "bin" / "gate-checkpoint"
        # This command may not exist yet, so we'll check and skip if not
        if script_path.exists():
            assert os.access(script_path, os.X_OK)
        else:
            pytest.skip("gate-checkpoint not implemented yet")

    @patch('lmer_cli.gates.GateSystem.run_commit_gate')
    def test_gate_check_command(self, mock_run):
        """Test running gate-check command"""
        mock_run.return_value = True

        from lmer_cli.gates import commit_gate
        result = commit_gate(verbose=False)
        assert result == 0
        mock_run.assert_called_once()

    def test_gate_commit_command_success(self):
        """Test running gate-commit command when checks pass"""
        # Test that the script can be executed
        script_path = Path(__file__).parent.parent / "bin" / "gate-commit"
        result = subprocess.run(
            [sys.executable, str(script_path), "-h"],
            capture_output=True,
            text=True
        )
        assert result.returncode == 0
        assert "Gate commit" in result.stdout or "message" in result.stdout

    def test_gate_commit_command_requires_message(self):
        """Test that gate-commit requires a message"""
        script_path = Path(__file__).parent.parent / "bin" / "gate-commit"
        result = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True
        )
        # Should fail without message
        assert result.returncode != 0
        assert "required" in result.stderr.lower()

    def _load_gate_commit_module(self):
        import importlib.util
        from importlib.machinery import SourceFileLoader
        script_path = Path(__file__).parent.parent / "bin" / "gate-commit"
        loader = SourceFileLoader("_gate_commit_script", str(script_path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module

    def test_cli_env_dict_declares_quick_gate_commit(self):
        """Guard: LMER_QUICK_GATE_COMMIT must be in cli.py's container env dict.

        Without this entry, setting LMER_QUICK_GATE_COMMIT=1 on the host has no
        effect because the var never reaches the container where gate-commit runs.
        """
        import re
        cli_py = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"
        source = cli_py.read_text()
        pattern = re.compile(
            r"""["']LMER_QUICK_GATE_COMMIT["']\s*:\s*os\.environ\.get\(\s*["']LMER_QUICK_GATE_COMMIT["']\s*\)"""
        )
        assert pattern.search(source), \
            "LMER_QUICK_GATE_COMMIT entry missing from cli.py container env dict"

    @patch('subprocess.run')
    @patch('lmer_cli.gates.GateSystem.run_commit_gate')
    def test_gate_commit_quick_env_var_skips_tests(self, mock_gate, mock_subproc, monkeypatch):
        """LMER_QUICK_GATE_COMMIT=1 forwards skip_tests=True to run_commit_gate."""
        mock_gate.return_value = True
        mock_subproc.return_value = MagicMock(returncode=0)
        monkeypatch.setenv("LMER_QUICK_GATE_COMMIT", "1")

        module = self._load_gate_commit_module()
        rc = module.gate_commit("test commit", verbose=False, bypass=False)

        assert rc == 0
        mock_gate.assert_called_once_with(skip_tests=True)

    @patch('subprocess.run')
    @patch('lmer_cli.gates.GateSystem.run_commit_gate')
    def test_gate_commit_default_runs_tests(self, mock_gate, mock_subproc, monkeypatch):
        """Without LMER_QUICK_GATE_COMMIT=1, skip_tests defaults to False."""
        mock_gate.return_value = True
        mock_subproc.return_value = MagicMock(returncode=0)
        monkeypatch.delenv("LMER_QUICK_GATE_COMMIT", raising=False)

        module = self._load_gate_commit_module()
        rc = module.gate_commit("test commit", verbose=False, bypass=False)

        assert rc == 0
        mock_gate.assert_called_once_with(skip_tests=False)

    @patch('subprocess.run')
    def test_gate_commit_alone_enables_post_commit_cache_handoff(
            self, mock_subproc, monkeypatch):
        mock_subproc.return_value = MagicMock(returncode=0)
        monkeypatch.delenv("LMER_QUICK_GATE_COMMIT", raising=False)
        module = self._load_gate_commit_module()
        gate = MagicMock()
        gate.run_commit_gate.return_value = True
        gate.handoff_test_cache_after_commit.return_value = False
        constructor = MagicMock(return_value=gate)
        module.GateSystem = constructor

        assert module.gate_commit("test commit", verbose=False, bypass=False) == 0

        constructor.assert_called_once_with(verbose=False, commit_handoff=True)

    @patch('subprocess.run')
    @patch('lmer_cli.gates.GateSystem.run_commit_gate')
    def test_gate_commit_quick_truthy_strings_enable(self, mock_gate, mock_subproc, monkeypatch):
        """Truthy strings (true/yes, case-insensitive) all enable quick mode."""
        mock_subproc.return_value = MagicMock(returncode=0)
        for value in ("1", "true", "TRUE", "yes", "Yes"):
            mock_gate.reset_mock()
            mock_gate.return_value = True
            monkeypatch.setenv("LMER_QUICK_GATE_COMMIT", value)
            module = self._load_gate_commit_module()
            module.gate_commit("msg", verbose=False, bypass=False)
            mock_gate.assert_called_once_with(skip_tests=True), f"value={value!r}"

    @patch('subprocess.run')
    @patch('lmer_cli.gates.GateSystem.run_commit_gate')
    def test_gate_commit_quick_falsy_strings_disable(self, mock_gate, mock_subproc, monkeypatch):
        """Falsy strings (0/false/no) keep tests running — supports `export X=0` as off-switch."""
        mock_subproc.return_value = MagicMock(returncode=0)
        for value in ("0", "false", "FALSE", "no", "No"):
            mock_gate.reset_mock()
            mock_gate.return_value = True
            monkeypatch.setenv("LMER_QUICK_GATE_COMMIT", value)
            module = self._load_gate_commit_module()
            module.gate_commit("msg", verbose=False, bypass=False)
            mock_gate.assert_called_once_with(skip_tests=False), f"value={value!r}"

    @patch('lmer_cli.gates.GateSystem.run_push_gate')
    def test_gate_push_command_success(self, mock_run):
        """Test running gate-push command when allowed"""
        mock_run.return_value = True

        from lmer_cli.gates import push_gate
        result = push_gate(verbose=False)
        assert result == 0

    @patch('lmer_cli.gates.GateSystem.run_push_gate')
    def test_gate_push_command_blocked(self, mock_run):
        """Test running gate-push command when blocked"""
        mock_run.return_value = False

        from lmer_cli.gates import push_gate
        result = push_gate(verbose=False)
        assert result == 1


class TestGateIntegration:
    """Integration tests for the gate system"""

    def test_gate_system_integration(self, tmp_path, monkeypatch):
        """Test full gate system integration"""
        # Create a temporary git repo
        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init"], check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], check=True)

        # Create required documentation
        (tmp_path / "README.md").write_text("# Test Project")
        (tmp_path / "AGENTS.md").write_text("# Claude Config")
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "git.md").write_text("# Git Rules")
        (rules_dir / "testing.md").write_text("# Testing Rules")

        # Create a test file
        test_file = tmp_path / "test.py"
        test_file.write_text("def test():\n    return 42")

        # Create tests directory
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_main.py").write_text("def test_pass():\n    assert True")

        # Stage the test file
        subprocess.run(["git", "add", "test.py"], check=True)

        # Run gate system
        gate = GateSystem(verbose=True)
        gate.project_root = tmp_path

        # Check staged files
        result = gate.check_staged_files()
        assert result.status == CheckStatus.PASSED

        # Check documentation
        result = gate.check_documentation()
        assert result.status == CheckStatus.PASSED

    def test_gate_enforcement_prevents_git_add_all(self, tmp_path, monkeypatch):
        """Test that gate system detects git add -A abuse"""
        # Create a temporary git repo
        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init"], check=True)

        # Create various files including ones that shouldn't be staged
        (tmp_path / "main.py").write_text("print('main')")
        venv_dir = tmp_path / ".venv" / "lib"
        venv_dir.mkdir(parents=True)
        (venv_dir / "module.py").write_text("# venv file")
        cache_dir = tmp_path / "__pycache__"
        cache_dir.mkdir()
        (cache_dir / "main.cpython-311.pyc").write_text("# cache")

        # Simulate git add -A
        subprocess.run(["git", "add", "-A"], check=True)

        # Run gate check
        gate = GateSystem()
        gate.project_root = tmp_path
        result = gate.check_staged_files()

        # Should detect suspicious files
        assert result.status == CheckStatus.FAILED
        assert "Suspicious files staged" in result.message


class TestGateStructure:
    """Test that gates are properly structured in documentation."""

    def test_commit_gate_exists(self):
        """Verify COMMIT GATE exists with proper structure."""
        main_config = Path(__file__).parent.parent / "AGENTS.md"
        content = main_config.read_text()
        assert "## 🛑 COMMIT GATE" in content

        # Check for numbered steps - updated for gate commands
        assert "1. Run gate check:" in content
        assert "2. Show gate check results" in content
        assert "3. Get explicit approval:" in content
        assert "4. Use gate commit:" in content

    def test_error_gate_structure(self):
        """Verify ERROR GATE has proper format."""
        main_config = Path(__file__).parent.parent / "AGENTS.md"
        content = main_config.read_text()
        assert "## 🛑 ERROR GATE" in content

        # Check for required elements — the report block survives the
        # risk-based rewrite, scoped to the STOP cases.
        assert "❌ ERROR ENCOUNTERED:" in content
        assert "📍 WHERE:" in content
        assert "🔍 ANALYSIS:" in content
        assert "🔧 PROPOSED FIX:" in content
        assert "💭 WHY THIS FIX:" in content

    def test_error_gate_triggers_on_fix_cost_not_on_failure(self):
        """The gate keys on what the fix costs, not on any error occurring (#137).

        Gating every failure halts an agent on its own malformed commands — in a
        headless child that is a dropped result, not a pause. Visibility stays
        unconditional; only the narrow authorization cases stop.
        """
        content = (Path(__file__).parent.parent / "AGENTS.md").read_text()
        # Whitespace-normalized: these rules must survive a reflow or a
        # markdown formatter pass, which line-wrap columns and bullet indents
        # would not.
        flat = " ".join(content.split())

        assert "## 🛑 ERROR GATE - When a fix needs authorization" in content
        assert "## 🛑 ERROR GATE - When Something Fails" not in content

        # The no-gate classes must stay explicitly no-gate.
        assert "**Your own malformed command**" in flat
        assert "fix it and continue" in flat
        assert "**Environment or capability gap**" in flat

        # A missing binary is a capability gap (class 2), never class 1 —
        # class 2 carries the worked example that decides it.
        assert "missing shortcut binary" not in flat
        assert "`grep` for a missing `rg`" in flat

        # The gated classes, and the churn trigger the gate exists for.
        assert "when the fix would mutate state, is hard to reverse" in flat
        assert "when you do not understand the cause" in flat

        # Showing the error is never conditional on stopping.
        assert "Visibility is unconditional in all four cases" in flat

    def test_error_gate_report_has_non_interactive_closing(self):
        """The STOP template must not hand a headless agent a question to emit.

        The template is the most local, most concrete instruction at a STOP,
        so a bare `(yes/no)` closing line would out-argue the general rule 130
        lines above it (#137).
        """
        content = (Path(__file__).parent.parent / "AGENTS.md").read_text()
        flat = " ".join(content.split())

        assert "Shall I proceed with this fix? (yes/no)" in flat
        assert "In a non-interactive session, replace that closing question" in flat
        assert "⏸️ STOPPED — would have asked" in flat

        # The override has to follow the template it overrides.
        assert content.index("Shall I proceed with this fix?") < content.index(
            "In a non-interactive session, replace that closing question"
        )

    def test_non_interactive_section(self):
        """A headless session must report instead of ending its turn on a question."""
        content = (Path(__file__).parent.parent / "AGENTS.md").read_text()
        flat = " ".join(content.split())

        assert "## 🤖 NON-INTERACTIVE SESSIONS" in content
        assert "LMER_NONINTERACTIVE" in flat
        assert "no gate below may end your turn with a question" in flat
        # Not asking must not decay into doing it anyway.
        assert "Do NOT perform the gated action either" in flat

        # The clause has to precede the gates it governs.
        assert content.index("## 🤖 NON-INTERACTIVE SESSIONS") < content.index(
            "## 🛑 COMMIT GATE"
        )

    def test_non_interactive_section_states_truthy_contract(self):
        """The prose is the parsing contract — no Python reads this var (#137).

        Every other boolean LMER_* var documents `1`/`true`/`yes` on and
        `0`/`false`/`no` off; a strict `=1` reading would make `=true` silently
        mean "a human is present", which is the failure class this section is
        about.
        """
        flat = " ".join((Path(__file__).parent.parent / "AGENTS.md").read_text().split())

        assert "`1`, `true`, `yes`, case-insensitive" in flat
        assert "A falsy value (`0`, `false`, `no`) or an unset variable" in flat

    def test_non_interactive_section_carves_out_advance_approval(self):
        """Composed with the COMMIT GATE, no carve-out makes headless runs useless.

        cron/CI launches are exactly the runs whose purpose is to produce
        committed work, so the section has to say that approval granted before
        the session started still counts (#137).
        """
        flat = " ".join((Path(__file__).parent.parent / "AGENTS.md").read_text().split())

        assert "Approval already granted before the session started is still approval" in flat
        assert "covers approvals you would have to obtain *now*" in flat
        # And it must say what each gate does instead of stopping.
        assert "**CONTEXT SWITCH GATE** — state the switch" in flat
        assert "**COMMIT GATE** — run `gate-check`" in flat

    def test_cli_env_dict_declares_non_interactive(self):
        """Guard: LMER_NONINTERACTIVE must be in cli.py's container env dict.

        Without this entry, a cron wrapper exporting LMER_NONINTERACTIVE=1 on
        the host has no effect on the agent inside the container, where the
        AGENTS.md section above is what reads it. Lives beside that section's
        tests rather than in the spawn-harness module: the reader is AGENTS.md,
        not child-env composition.
        """
        cli_py = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"
        pattern = re.compile(
            r"""["']LMER_NONINTERACTIVE["']\s*:\s*os\.environ\.get\(\s*"""
            r"""["']LMER_NONINTERACTIVE["']\s*\)"""
        )
        assert pattern.search(cli_py.read_text()), \
            "LMER_NONINTERACTIVE entry missing from cli.py container env dict"

    def test_non_interactive_fragment_carries_the_rule(self):
        """The fragment is how the rule reaches a session at all (#137).

        Nothing renders an LMER_* value into a model's context, and claude
        discovers only CLAUDE.md natively — so for headless launches this file,
        not the variable, is the delivery. It must therefore restate the rule
        rather than point at AGENTS.md.
        """
        fragment = Path(__file__).parent.parent / "prompts" / "non-interactive.md"
        assert fragment.is_file(), "prompts/non-interactive.md is missing"
        flat = " ".join(fragment.read_text().split())

        assert "No gate may end your turn with a question" in flat
        assert "do not perform the gated action either" in flat
        assert "is still approval" in flat


    def test_context_switch_gate(self):
        """Verify CONTEXT SWITCH GATE exists."""
        main_config = Path(__file__).parent.parent / "AGENTS.md"
        content = main_config.read_text()
        assert "## 🛑 CONTEXT SWITCH GATE" in content

        # Check for checklist items
        assert "- [ ] Summarize what was just completed" in content
        assert "- [ ] Run rgr-[relevant module]" in content
        assert "- [ ] State the new objective clearly" in content


class TestCustomTestRunner:
    """Tests for project-supplied gate-check-run-tests.sh discovery."""

    def setup_method(self):
        self.gate = GateSystem(verbose=True)

    def _setup_work_repo(self, monkeypatch, work_root: Path,
                        host: str = "gitlab.example.com",
                        project: str = "group/example",
                        task: str = "develop") -> Path:
        """Configure LMER_* env so the gate looks at work_root, return project base."""
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work_root))
        monkeypatch.setenv("LMER_REPO_HOST", host)
        monkeypatch.setenv("LMER_REPO_PROJECT", project)
        monkeypatch.setenv("LMER_TASK", task)
        return work_root / host / project

    def _write_runner(self, path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        path.chmod(0o755)

    def test_no_runner_falls_back_to_pytest(self, tmp_path, monkeypatch):
        """When no custom runner is configured, the pytest path is used."""
        self._setup_work_repo(monkeypatch, tmp_path)
        # Point project_root at a dir without a tests/ folder so we hit the
        # "no tests directory" early return — proves we did NOT take the
        # custom-runner path.
        self.gate.project_root = tmp_path / "proj"
        self.gate.project_root.mkdir()
        result = self.gate.check_tests()
        assert result.status == CheckStatus.PASSED
        assert "No tests directory" in result.message

    def test_global_runner_invoked(self, tmp_path, monkeypatch):
        """Global info/gate-check-run-tests.sh is invoked when present."""
        base = self._setup_work_repo(monkeypatch, tmp_path)
        marker = tmp_path / "ran"
        self._write_runner(
            base / "info" / "gate-check-run-tests.sh",
            f"#!/bin/sh\necho global ok\ntouch {marker}\nexit 0\n",
        )

        self.gate.project_root = tmp_path
        result = self.gate.check_tests()

        assert result.status == CheckStatus.PASSED
        assert marker.exists()
        assert "Custom test runner passed" in result.message
        assert "gate-check-run-tests.sh" in result.message
        # Passing runner's output tail is surfaced for verbose feedback
        assert result.details is not None
        assert any("global ok" in line for line in result.details)

    def test_task_runner_overrides_global(self, tmp_path, monkeypatch):
        """Task-specific runner takes precedence over the global one."""
        base = self._setup_work_repo(monkeypatch, tmp_path, task="develop")
        global_marker = tmp_path / "global_ran"
        task_marker = tmp_path / "task_ran"
        self._write_runner(
            base / "info" / "gate-check-run-tests.sh",
            f"#!/bin/sh\ntouch {global_marker}\nexit 0\n",
        )
        self._write_runner(
            base / "develop" / "info" / "gate-check-run-tests.sh",
            f"#!/bin/sh\ntouch {task_marker}\nexit 0\n",
        )

        self.gate.project_root = tmp_path
        result = self.gate.check_tests()

        assert result.status == CheckStatus.PASSED
        assert task_marker.exists()
        assert not global_marker.exists()

    def test_runner_failure_maps_to_failed(self, tmp_path, monkeypatch):
        """A non-zero exit from the runner produces a FAILED result with output tail."""
        base = self._setup_work_repo(monkeypatch, tmp_path)
        self._write_runner(
            base / "info" / "gate-check-run-tests.sh",
            "#!/bin/sh\necho line-a\necho line-b\necho line-c 1>&2\nexit 7\n",
        )

        self.gate.project_root = tmp_path
        result = self.gate.check_tests()

        assert result.status == CheckStatus.FAILED
        assert "Custom test runner failed" in result.message
        # Output is captured and tail surfaced as details — all three lines
        # fit in the 5-line tail, so all three should be present (catches a
        # regression where stderr stops being merged with stdout).
        assert result.details is not None
        joined = "\n".join(result.details)
        assert all(line in joined for line in ("line-a", "line-b", "line-c"))

    def test_non_executable_runner_is_ignored(self, tmp_path, monkeypatch):
        """A non-executable script is not used; pytest path is taken instead."""
        base = self._setup_work_repo(monkeypatch, tmp_path)
        script = base / "info" / "gate-check-run-tests.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o644)  # not executable

        self.gate.project_root = tmp_path / "proj"
        self.gate.project_root.mkdir()
        result = self.gate.check_tests()
        # Falls through to "no tests directory" — proves pytest path was taken
        assert result.status == CheckStatus.PASSED
        assert "No tests directory" in result.message

    def test_missing_env_returns_none(self, tmp_path, monkeypatch):
        """Without LMER_REPO_HOST/PROJECT, discovery is skipped."""
        monkeypatch.delenv("LMER_REPO_HOST", raising=False)
        monkeypatch.delenv("LMER_REPO_PROJECT", raising=False)
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
        assert self.gate._find_custom_test_runner() is None


class TestResolvePrecommitCommand:
    """Tests for `_resolve_precommit_command` fallback chain."""

    def setup_method(self):
        self.gate = GateSystem(verbose=True)

    def test_prefers_venv_binary(self, tmp_path):
        """A `.venv/bin/pre-commit` always wins over manager fallbacks."""
        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        binary = venv_bin / "pre-commit"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        # Even with a uv project signal, venv binary wins
        (tmp_path / "uv.lock").write_text("")

        self.gate.project_root = tmp_path
        with patch("shutil.which", return_value="/usr/bin/uv"):
            assert self.gate._resolve_precommit_command() == [str(binary)]

    def test_venv_with_broken_shebang_falls_through_to_bare(self, tmp_path):
        """A venv binary whose shebang interpreter is missing is skipped.

        Reproduces the bind-mounted-host-venv case: the script is on disk
        (`.exists()` is True) but its shebang points at an interpreter path
        that doesn't resolve here, so `subprocess.run` would fail with ENOENT.
        """
        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        binary = venv_bin / "pre-commit"
        binary.write_text(
            "#!/nonexistent/host/path/.venv/bin/python3\nimport sys\n"
        )
        binary.chmod(0o755)

        self.gate.project_root = tmp_path
        assert self.gate._resolve_precommit_command() == ["pre-commit"]

    def test_no_signals_uses_bare_precommit(self, tmp_path):
        """No venv binary → bare `pre-commit`."""
        self.gate.project_root = tmp_path
        assert self.gate._resolve_precommit_command() == ["pre-commit"]

    def test_uv_lock_does_not_trigger_uv_run(self, tmp_path):
        """Regression: presence of uv.lock must NOT cause `uv run pre-commit`.

        `uv run` syncs the whole project env before executing, which fails
        on any project whose runtime deps need system libraries the gate
        environment can't supply (e.g. `mysqlclient` needing MySQL dev
        headers). pre-commit doesn't need
        the project env at all — its hook envs are pinned by
        `.pre-commit-config.yaml` and managed independently.
        """
        (tmp_path / "uv.lock").write_text("")
        self.gate.project_root = tmp_path
        assert self.gate._resolve_precommit_command() == ["pre-commit"]

    def test_poetry_lock_does_not_trigger_poetry_run(self, tmp_path):
        """Same regression as uv.lock, for poetry-managed projects."""
        (tmp_path / "poetry.lock").write_text("")
        self.gate.project_root = tmp_path
        assert self.gate._resolve_precommit_command() == ["pre-commit"]


class TestPushAllowGrammar:
    """The repo half of an allow-list entry (#107).

    The verdicts every enforcement point must agree on live in
    tests/test_push_allow_grammar_parity.py; this class pins the module's
    own API, including the push-by-URL branch that has no mirror in
    hooks/pc.py.
    """

    @pytest.mark.parametrize("entry,url,expected", [
        # Exact repo — SSH/HTTPS/bare spellings are interchangeable.
        ("git@gitlab.example.com:group/project.git",
         "git@gitlab.example.com:group/project.git", True),
        ("gitlab.example.com/group/project",
         "https://gitlab.example.com/group/project.git", True),
        ("https://gitlab.example.com/group/project",
         "git@gitlab.example.com:group/project.git", True),
        ("gitlab.example.com/group/project",
         "git@gitlab.example.com:group/other.git", False),
        # Whole host — any project on the EXACT host, not a suffix of it.
        ("gitlab.example.com", "git@gitlab.example.com:anything/at/all.git", True),
        ("gitlab.example.com", "git@sub.gitlab.example.com:x/y.git", False),
        ("gitlab.example.com", "git@github.com:user/repo.git", False),
        # Wildcard domain — subdomains only, dot boundary enforced.
        ("*.example.com", "git@gitlab.example.com:x/y.git", True),
        ("*.example.com", "https://code.example.com/a/b", True),
        ("*.example.com", "git@example.com:x/y.git", False),      # apex
        ("*.example.com", "git@evilexample.com:x/y.git", False),  # dot boundary
        # Host + project prefix — segment-boundary safe.
        ("gitlab.example.com/group", "git@gitlab.example.com:group/project.git", True),
        ("gitlab.example.com/group",
         "https://gitlab.example.com/group/project/sub.git", True),
        ("gitlab.example.com/group", "git@gitlab.example.com:groupfoo/x.git", False),
        ("gitlab.example.com/group", "git@other.example.com:group/project.git", False),
        ("*.example.com/group", "git@code.example.com:group/project.git", True),
        # Legacy host-less project path — any host, segment boundary.
        ("org/repo1", "git@github.com:org/repo1.git", True),
        ("org/repo1", "https://gitlab.example.com/org/repo1", True),
        ("org/repo1", "git@github.com:org/repo10.git", False),
        # Hosts compare case-insensitively, project paths do not.
        ("GITLAB.example.COM", "git@gitlab.example.com:x/y.git", True),
        ("gitlab.example.com/Group/Project",
         "git@gitlab.example.com:group/project.git", False),
        # A host that merely EMBEDS the allowed path grants nothing — the
        # substring rule this grammar replaced allowed exactly this.
        ("org/repo1", "https://evil.example.com/mirror/org/repo1.git", False),
        # IPv6 literals: the bracket bounds the host, so the address's own
        # colons are not the `host:path` delimiter. Cutting at the first
        # colon instead makes every `2001:…` address one host.
        ("[2001:db8::1]/group/project",
         "https://[2001:db8::1]/group/project", True),
        ("[2001:db8::1]/group/project",
         "git@[2001:db8::1]:group/project.git", True),
        ("[2001:db8::1]", "https://[2001:db8::1]/anything/at/all", True),
        ("[2001:db8::1]", "https://[2001:db8::9999]/group/project", False),
        ("[::1]/group/project", "https://[::2]/group/project", False),
        ("[2001", "https://[2001:db8::1]/group/project", False),
        # An unclosed bracket names no host, in the entry or in the target.
        ("[2001:db8::1", "https://[2001:db8::1]/group/project", False),
        ("group/project", "https://[::1/group/project", False),
    ])
    def test_matrix(self, entry, url, expected):
        from lmer_cli import push_allow
        assert push_allow.target_allowed(url, [entry]) is expected

    def test_union_any_entry_grants(self):
        from lmer_cli import push_allow
        entries = ["nope.example.com", "org/repo", "other.example.com"]
        assert push_allow.target_allowed("git@github.com:org/repo.git", entries)

    def test_granting_entry_names_the_winner(self):
        from lmer_cli import push_allow
        entries = ["nope.example.com", "org/repo"]
        assert push_allow.granting_entry(
            "git@github.com:org/repo.git", entries) == "org/repo"

    def test_empty_allow_list_refuses(self):
        from lmer_cli import push_allow
        assert not push_allow.target_allowed("git@github.com:org/repo.git", [])

    @pytest.mark.parametrize("url", [
        "/srv/git/repo.git",          # filesystem remote: no host
        "https://gitlab.example.com", # host but no project
    ])
    def test_unidentifiable_target_refuses(self, url):
        """A target that does not parse into host+path cannot be named by
        any entry, so it is refused rather than guessed at. (#107's first
        cut fell back to a substring test here; that fallback is gone —
        the gate says so in its refusal message instead.)"""
        from lmer_cli import push_allow
        assert not push_allow.target_allowed(url, ["repo", "gitlab.example.com"])

    def test_example_entry_rebrackets_an_ipv6_host(self):
        """The refusal's copy-pasteable example must PARSE as an entry. The
        normalized host is the bare address, but the bare spelling is not a
        legal entry — `2001:db8::1/group/project` reads as host `2001`
        under the scp-like rule — so the example has to re-bracket it, or
        pasting it earns a second refusal."""
        from lmer_cli import push_allow
        example = push_allow.example_entry(
            "https://[2001:db8::1]/group/project.git")
        assert example == "[2001:db8::1]/group/project"
        # ...and it round-trips: the entry it prints grants the push.
        assert push_allow.target_allowed(
            "https://[2001:db8::1]/group/project.git", [example])
        # IPv4 and DNS hosts are untouched by the re-bracketing.
        assert push_allow.example_entry(
            "git@gitlab.example.com:x/y.git") == "gitlab.example.com/x/y"

    @pytest.mark.parametrize("entry,expected", [
        ("[2001:db8::1]/group/project", True),   # full host/path
        ("[2001:db8::1]", True),                 # bare host
        ("[2001:db8::9999]/group/project", False),  # a different address
        ("[2001", False),                        # not a host prefix
    ])
    def test_push_by_url_pins_the_ipv6_address(self, entry, expected):
        """The anchored branch pins the whole address, not its first
        hextet: the entry must name the identity git will dial."""
        from lmer_cli import push_allow
        assert push_allow.target_allowed(
            "https://[2001:db8::1]/group/project", [entry],
            exact_identity=True) is expected

    @pytest.mark.parametrize("entry,expected", [
        ("gitlab.example.com/group/project", True),   # full host/path
        ("gitlab.example.com", True),                 # bare host
        ("group/project", False),                     # host-less: no host pinned
        ("*.example.com", False),                     # wildcard: no host pinned
        ("gitlab.example.com/group", False),          # prefix: not the identity
    ])
    def test_push_by_url_requires_the_exact_identity(self, entry, expected):
        """On the push-by-URL branch the URL is agent-supplied, so only an
        entry naming the exact identity git will dial grants. The grammar
        added by #107 must never widen this branch."""
        from lmer_cli import push_allow
        assert push_allow.target_allowed(
            "https://gitlab.example.com/group/project.git", [entry],
            exact_identity=True) is expected


class TestPushGateTaskdefUnion:
    """gate-push unions LMER_PUSH_ALLOW_LIST with the taskdef's task.yaml
    push_allow list — resolved from the trusted taskdef tiers only, never
    the agent-writable work-repo tiers (#107)."""

    def setup_method(self):
        self.gate = GateSystem(verbose=True)

    def _isolate(self, monkeypatch, tmp_path, task="pushtask",
                 manifest=None, work_manifest=None):
        """Point the taskdef search at tmp tiers, away from ambient config.

        ``manifest`` lands in a trusted tier (an LMER_TASKDEF_PATHS dir);
        ``work_manifest`` lands in BOTH work-repo tiers (project-scoped and
        work-global), which must never contribute push grants. Returns the
        trusted manifest path.
        """
        trusted = tmp_path / "trusted-taskdefs"
        trusted_task = trusted / task
        trusted_task.mkdir(parents=True, exist_ok=True)
        if manifest is not None:
            (trusted_task / "task.yaml").write_text(manifest)

        work = tmp_path / "work"
        host, project = "git.example.com", "group/proj"
        for tier in (work / host / project / "taskdef", work / "taskdef"):
            task_dir = tier / task
            task_dir.mkdir(parents=True, exist_ok=True)
            if work_manifest is not None:
                (task_dir / "task.yaml").write_text(work_manifest)

        monkeypatch.setenv("LMER_TASKDEF_PATHS", str(trusted))
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
        monkeypatch.setenv("LMER_REPO_HOST", host)
        monkeypatch.setenv("LMER_REPO_PROJECT", project)
        monkeypatch.setenv("LMER_TASK", task)
        monkeypatch.delenv("LMER_PUSH_ALLOW_LIST", raising=False)
        for var in ("LMER_TASKDEF_DIR", "LMER_TASK_INSTRUCTIONS", "LMER_TASKDEF"):
            monkeypatch.delenv(var, raising=False)
        return trusted_task / "task.yaml"

    def _mock_git(self, url="git@gitlab.example.com:x/y.git", branch="feature-x"):
        def fake_run(command, **kwargs):
            if command[:3] == ["git", "remote", "get-url"]:
                return MagicMock(returncode=0, stdout=url + "\n", stderr="")
            if command[:3] == ["git", "branch", "--show-current"]:
                return MagicMock(returncode=0, stdout=branch + "\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        return fake_run

    def test_reads_trusted_task_yaml(self, monkeypatch, tmp_path):
        from lmer_cli import push_allow
        self._isolate(monkeypatch, tmp_path,
                      manifest="push_allow:\n  - gitlab.example.com\n  - org/repo\n")
        assert push_allow.taskdef_allow_list() == ["gitlab.example.com", "org/repo"]

    def test_reports_manifest_path(self, monkeypatch, tmp_path):
        from lmer_cli import push_allow
        manifest_path = self._isolate(monkeypatch, tmp_path,
                                      manifest="push_allow:\n  - org/repo\n")
        assert push_allow.taskdef_allow_source() == (["org/repo"], manifest_path)

    def test_work_repo_tier_manifest_never_grants(self, monkeypatch, tmp_path):
        """The work repo is pushed to by every session, so a task.yaml
        smuggled into it must not be able to grant itself push targets."""
        from lmer_cli import push_allow
        self._isolate(monkeypatch, tmp_path,
                      work_manifest="push_allow:\n  - '*.example.com'\n")
        assert push_allow.taskdef_allow_source() == ([], None)

    def test_trusted_tier_wins_over_work_repo_override(self, monkeypatch, tmp_path):
        from lmer_cli import push_allow
        self._isolate(monkeypatch, tmp_path,
                      manifest="push_allow:\n  - org/trusted\n",
                      work_manifest="push_allow:\n  - '*.example.com'\n")
        assert push_allow.taskdef_allow_list() == ["org/trusted"]

    def test_pre_resolved_dir_fallback_never_grants(self, monkeypatch, tmp_path):
        """LMER_TASKDEF_DIR may point into a work-repo tier, so it is not a
        grant source either."""
        from lmer_cli import push_allow
        self._isolate(monkeypatch, tmp_path)
        work_task = tmp_path / "work" / "taskdef" / "pushtask"
        (work_task / "task.yaml").write_text("push_allow:\n  - '*.example.com'\n")
        monkeypatch.setenv("LMER_TASKDEF_DIR", str(work_task))
        assert push_allow.taskdef_allow_source() == ([], None)

    @pytest.mark.parametrize("manifest", [
        "",                                   # empty file
        "not-a-mapping\n",                    # scalar document
        "push_allow: {}\n",                   # wrong node type
        "push_allow:\n  - [a, b]\n",          # non-string member
        "push_allow: [unclosed\n",            # malformed YAML
    ])
    def test_fail_soft(self, monkeypatch, tmp_path, manifest):
        """A bad manifest must never grant, and never crash the gate."""
        from lmer_cli import push_allow
        self._isolate(monkeypatch, tmp_path, manifest=manifest)
        assert push_allow.taskdef_allow_source()[0] == []

    @pytest.mark.parametrize("key", ["push_targets", "push_allow"])
    def test_nested_block_is_never_a_grant(self, monkeypatch, tmp_path, key):
        """taskdef/release/task.yaml carries a documentation-only list of
        ROLE mappings nested under `needs:`. It is spelled
        `needs.push_targets` so it cannot be confused with the grant key at
        all (docs/TASKDEFS.md) — but the resolver must ignore a nested
        block under EITHER spelling, and the mapping shape must not be
        stringified into a junk entry if one ever moved to the top level."""
        from lmer_cli import push_allow
        self._isolate(monkeypatch, tmp_path, manifest=(
            "needs:\n"
            f"  {key}:\n"
            "    - target: github_mirror\n"
            "      refs:\n"
            "        - refs/tags/*\n"
        ))
        assert push_allow.taskdef_allow_source() == ([], None)

    @patch('subprocess.run')
    def test_gate_allows_via_taskdef_grant(self, mock_run, monkeypatch,
                                           tmp_path, capsys):
        manifest_path = self._isolate(
            monkeypatch, tmp_path,
            manifest="push_allow:\n  - gitlab.example.com/x\n")
        mock_run.side_effect = self._mock_git()
        self.gate.run_commit_gate = MagicMock(return_value=True)

        assert self.gate.run_push_gate() is True
        out = capsys.readouterr().out
        assert "granted by 'gitlab.example.com/x'" in out
        assert f"task.yaml @ {manifest_path}" in out

    @patch('subprocess.run')
    def test_gate_refuses_work_repo_tier_grant(self, mock_run, monkeypatch,
                                               tmp_path):
        self._isolate(monkeypatch, tmp_path,
                      work_manifest="push_allow:\n  - '*.example.com'\n")
        mock_run.side_effect = self._mock_git()
        assert self.gate.run_push_gate() is False

    @patch('subprocess.run')
    def test_taskdef_entry_is_branch_only_unless_ref_scoped(
            self, mock_run, monkeypatch, tmp_path):
        """A taskdef grant obeys the same ref rule as an env one: bare
        entries authorize branches, never tags."""
        self._isolate(monkeypatch, tmp_path,
                      manifest="push_allow:\n  - gitlab.example.com\n")
        mock_run.side_effect = self._mock_git()
        self.gate.run_commit_gate = MagicMock(return_value=True)

        assert self.gate.run_push_gate(ref="refs/tags/v1.0") is False
        assert self.gate.run_push_gate(ref="refs/heads/main") is True

    @patch('subprocess.run')
    def test_taskdef_entry_may_scope_a_tag_push(self, mock_run, monkeypatch,
                                                tmp_path):
        self._isolate(
            monkeypatch, tmp_path,
            manifest="push_allow:\n  - 'gitlab.example.com|refs/tags/*'\n")
        mock_run.side_effect = self._mock_git()
        self.gate.run_commit_gate = MagicMock(return_value=True)

        assert self.gate.run_push_gate(ref="refs/tags/v1.0") is True


class TestPushGateTransparency:
    """Both the grant and the refusal name the target, the sources consulted
    and the entry involved (#107). The grant path matters most: a grant
    arriving from a taskdef manifest rather than the operator's env is
    exactly the case worth seeing."""

    def setup_method(self):
        self.gate = GateSystem(verbose=True)

    @pytest.fixture(autouse=True)
    def _no_ambient_taskdef(self, monkeypatch, tmp_path):
        """The taskdef half of the union must not be whatever happens to be
        installed: `test_refusal_names_sources_and_an_example_entry` asserts
        "(none declared)", and an ambient `push_allow` would union into
        every verdict here. Same isolation as
        TestPushGateTaskdefUnion._isolate."""
        monkeypatch.setenv("LMER_TASKDEF_PATHS", str(tmp_path / "empty"))
        for var in ("LMER_TASK", "LMER_TASKDEF", "LMER_TASKDEF_DIR",
                    "LMER_TASK_INSTRUCTIONS"):
            monkeypatch.delenv(var, raising=False)

    def _mock_git(self, urls, branch="feature-x"):
        def fake_run(command, **kwargs):
            if command[:3] == ["git", "remote", "get-url"]:
                return MagicMock(returncode=0, stdout="\n".join(urls) + "\n",
                                 stderr="")
            if command[:3] == ["git", "branch", "--show-current"]:
                return MagicMock(returncode=0, stdout=branch + "\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        return fake_run

    @patch.dict(os.environ, {"LMER_PUSH_ALLOW_LIST": "gitlab.example.com"})
    @patch('subprocess.run')
    def test_grant_names_entry_and_source(self, mock_run, capsys):
        mock_run.side_effect = self._mock_git(["git@gitlab.example.com:x/y.git"])
        self.gate.run_commit_gate = MagicMock(return_value=True)

        assert self.gate.run_push_gate() is True
        out = capsys.readouterr().out
        assert "Push target allowed" in out
        assert "granted by 'gitlab.example.com' from LMER_PUSH_ALLOW_LIST" in out
        assert "refs/heads/feature-x" in out

    @patch.dict(os.environ, {"LMER_PUSH_ALLOW_LIST": "other.example.com"})
    @patch('subprocess.run')
    def test_refusal_names_sources_and_an_example_entry(self, mock_run, capsys):
        mock_run.side_effect = self._mock_git(["git@gitlab.example.com:x/y.git"])

        assert self.gate.run_push_gate() is False
        out = capsys.readouterr().out
        assert "Sources checked:" in out
        assert "LMER_PUSH_ALLOW_LIST: other.example.com" in out
        assert "taskdef task.yaml push_allow: (none declared)" in out
        assert "Example entry that would allow this push: gitlab.example.com/x/y" in out

    @patch.dict(os.environ, {"LMER_PUSH_ALLOW_LIST": "gitlab.example.com"})
    @patch('subprocess.run')
    def test_refusal_example_carries_the_ref_half_for_a_tag(self, mock_run, capsys):
        """A bare entry authorizes branches only, so the example offered for
        a refused TAG push must carry the ref half — otherwise copy-pasting
        it produces a second refusal."""
        mock_run.side_effect = self._mock_git(["git@other.example.com:x/y.git"])

        assert self.gate.run_push_gate(ref="refs/tags/v1.0") is False
        out = capsys.readouterr().out
        assert "other.example.com/x/y|refs/tags/v1.0" in out

    @patch.dict(os.environ, {"LMER_PUSH_ALLOW_LIST": "repo"})
    @patch('subprocess.run')
    def test_refusal_explains_an_unidentifiable_target(self, mock_run, capsys):
        """A filesystem remote parses into no host/path, so no entry can
        name it. Say that, instead of printing an example that cannot work."""
        mock_run.side_effect = self._mock_git(["/srv/git/repo.git"])

        assert self.gate.run_push_gate() is False
        assert "does not parse into host/path" in capsys.readouterr().out

    @patch.dict(os.environ, {"LMER_PUSH_ALLOW_LIST": "gitlab.example.com"})
    @patch('subprocess.run')
    def test_every_push_url_must_be_granted(self, mock_run, capsys):
        """`git push` sends the ref to EVERY pushurl of the remote, so one
        unallowlisted pushurl is one unauthorized push — even though the
        other one is allowed."""
        mock_run.side_effect = self._mock_git([
            "git@gitlab.example.com:x/y.git",
            "git@evil.example.org:x/y.git",
        ])

        assert self.gate.run_push_gate() is False
        out = capsys.readouterr().out
        assert "evil.example.org" in out
        assert "not allowed" in out

    @patch.dict(os.environ,
                {"LMER_PUSH_ALLOW_LIST": "gitlab.example.com,evil.example.org"})
    @patch('subprocess.run')
    def test_all_push_urls_granted_passes(self, mock_run):
        mock_run.side_effect = self._mock_git([
            "git@gitlab.example.com:x/y.git",
            "git@evil.example.org:x/y.git",
        ])
        self.gate.run_commit_gate = MagicMock(return_value=True)

        assert self.gate.run_push_gate() is True

    @patch.dict(os.environ, {"LMER_PUSH_ALLOW_LIST": "gitlab.example.com"})
    @patch('subprocess.run')
    def test_the_same_pushurl_listed_twice_still_passes(self, mock_run):
        """`git remote set-url --add --push origin <url>` run twice — a
        re-run setup script — leaves the SAME pushurl on the remote twice,
        and `get-url --push --all` prints both lines. The verdict must key
        on whether any URL was REFUSED, not on a count: grants are keyed by
        URL, so two identical lines collapse to one grant and a count
        comparison could never be satisfied — refusing a push whose every
        target is allowed, with no URL marked as the reason."""
        dup = "git@gitlab.example.com:x/y.git"
        mock_run.side_effect = self._mock_git([dup, dup])
        self.gate.run_commit_gate = MagicMock(return_value=True)

        assert self.gate.run_push_gate() is True

    @patch.dict(os.environ, {"LMER_PUSH_ALLOW_LIST": "other.example.com"})
    @patch('subprocess.run')
    def test_refusal_marks_every_denied_url_including_duplicates(
            self, mock_run, capsys):
        """The refusal must remain explainable when a duplicate pushurl is
        the one refused: every denied line marked, and an example entry
        offered (both are driven off the denial list, which is why it is a
        list rather than the complement of the grants dict)."""
        dup = "git@gitlab.example.com:x/y.git"
        mock_run.side_effect = self._mock_git([dup, dup])

        assert self.gate.run_push_gate() is False
        out = capsys.readouterr().out
        assert out.count("<-- not allowed") == 2
        assert ("Example entry that would allow this push: "
                "gitlab.example.com/x/y") in out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
