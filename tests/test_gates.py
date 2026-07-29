#!/usr/bin/env python3
"""
Tests for the gate system and gate commands
"""

import pytest
import subprocess
import os
import sys
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock, call

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lmer_cli.gates import GateSystem, CheckStatus, CheckResult, Colors


class TestGateSystem:
    """Test the GateSystem class functionality"""

    def setup_method(self):
        """Set up test environment"""
        self.gate = GateSystem(verbose=True)

    def test_init(self):
        """Test GateSystem initialization"""
        assert self.gate.verbose == True
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

    # NB: SSH remote URLs spell the repo `github.com:user/other-repo` — the
    # repo half is a plain substring, so entries name the path (`user/repo`).
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

        # Check for required elements
        assert "❌ ERROR ENCOUNTERED:" in content
        assert "📍 WHERE:" in content
        assert "🔍 ANALYSIS:" in content
        assert "🔧 PROPOSED FIX:" in content
        assert "💭 WHY THIS FIX:" in content

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
