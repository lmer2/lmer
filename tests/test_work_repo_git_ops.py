#!/usr/bin/env python3
"""Tests for work_repo.git_ops module"""

import os
import pytest
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

from work_repo.git_ops import run_git_command, commit_work_changes, commit_work_path


class TestRunGitCommand:
    """Test run_git_command function"""

    def test_run_git_command_success(self):
        """Test successful git command"""
        with tempfile.TemporaryDirectory() as tmpdir:
            rc, output = run_git_command(["--version"], Path(tmpdir), check=False)
            assert rc == 0
            assert "git version" in output.lower()

    def test_run_git_command_failure(self):
        """Test failed git command"""
        with tempfile.TemporaryDirectory() as tmpdir:
            rc, output = run_git_command(["invalid-command"], Path(tmpdir), check=False)
            assert rc != 0


class TestCommitWorkChanges:
    """Test commit_work_changes function"""

    def test_commit_work_changes_missing_env_vars(self):
        """Test commit when env vars are missing"""
        with patch.dict(os.environ, {}, clear=True):
            result = commit_work_changes()
            assert result == 1

    def test_commit_work_changes_repo_not_found(self):
        """Test commit when work repo doesn't exist"""
        env_vars = {
            "LMER_WORK_REPO_PATH": "/nonexistent/path",
            "LMER_REPO_HOST": "github.com",
            "LMER_REPO_PROJECT": "owner/repo",
            "LMER_TASK": "review",
            "LMER_TASK_TARGET": "pr-123",
        }

        with patch.dict(os.environ, env_vars):
            result = commit_work_changes()
            assert result == 1

    def test_commit_work_changes_no_changes(self):
        """Test commit when there are no changes"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Initialize git repo
            subprocess.run(["git", "init"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmpdir, check=True, capture_output=True)

            # Create directory structure
            target_dir = Path(tmpdir) / "github.com" / "owner" / "repo" / "review" / "pr-123"
            target_dir.mkdir(parents=True)

            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
                "LMER_TASK_TARGET": "pr-123",
            }

            with patch.dict(os.environ, env_vars):
                result = commit_work_changes()
                # Should return 0 (no changes to commit)
                assert result == 0

    def test_commit_work_changes_with_changes(self):
        """Test commit when there are changes"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Initialize git repo
            subprocess.run(["git", "init"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmpdir, check=True, capture_output=True)

            # Create initial commit
            (Path(tmpdir) / "README.md").write_text("Initial")
            subprocess.run(["git", "add", "README.md"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=tmpdir, check=True, capture_output=True)

            # Create directory structure and file
            target_dir = Path(tmpdir) / "github.com" / "owner" / "repo" / "review" / "pr-123"
            target_dir.mkdir(parents=True)
            (target_dir / "log.yaml").write_text("- message: test\n")

            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
                "LMER_TASK_TARGET": "pr-123",
            }

            with patch.dict(os.environ, env_vars):
                # Mock push to avoid needing remote
                with patch("work_repo.git_ops.run_git_command") as mock_git:
                    def git_side_effect(cmd, cwd, check=False):
                        if cmd == ["fetch"]:
                            return (0, "")
                        elif cmd == ["pull"]:
                            return (0, "")
                        elif cmd == ["add", "-A", "--", "github.com/owner/repo/review/pr-123"]:
                            return (0, "")
                        elif cmd == ["status", "--porcelain", "--", "github.com/owner/repo/review/pr-123"]:
                            return (0, "M  github.com/owner/repo/review/pr-123/log.yaml\n")
                        elif cmd == ["commit", "-m", "Update work repo: github.com/owner/repo/review/pr-123"]:
                            return (0, "")
                        elif cmd == ["push"]:
                            return (0, "")
                        return (0, "")

                    mock_git.side_effect = git_side_effect
                    result = commit_work_changes()
                    assert result == 0

    def test_commit_work_path_repo_not_found(self):
        """commit_work_path returns 1 when the work repo path doesn't exist."""
        with patch.dict(os.environ, {"LMER_WORK_REPO_PATH": "/nonexistent/path"}):
            assert commit_work_path("some/rel/path") == 1

    def test_commit_work_path_stages_given_path(self):
        """commit_work_path stages exactly the path it is given, with -A."""
        rel = "git.example.com/group/proj/memory"
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"LMER_WORK_REPO_PATH": tmpdir}):
                with patch("work_repo.git_ops.run_git_command") as mock_git:
                    def git_side_effect(cmd, cwd, check=False):
                        if cmd == ["status", "--porcelain", "--", rel]:
                            return (0, "M  git.example.com/group/proj/memory/fact.md\n")
                        return (0, "")

                    mock_git.side_effect = git_side_effect
                    result = commit_work_path(rel)
                    assert result == 0
                    add_calls = [
                        call for call in mock_git.call_args_list
                        if call[0][0] == ["add", "-A", "--", rel]
                    ]
                    assert len(add_calls) == 1

    def test_commit_work_path_no_change_in_target_ignores_other_dirty_files(self):
        """Unchanged target + an unrelated dirty file → return 0, no commit.

        Regression test: the no-change check must be scoped to target_path so a
        dirty per-session log.yaml elsewhere can't trigger a spurious empty
        commit (which would fail and return non-zero).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            subprocess.run(["git", "init"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmpdir, check=True, capture_output=True)

            mem_dir = tmp / "git.example.com" / "grp" / "proj" / "memory"
            mem_dir.mkdir(parents=True)
            (mem_dir / "fact.md").write_text("stable fact\n")
            other = tmp / "git.example.com" / "grp" / "proj" / "develop" / "issue-1"
            other.mkdir(parents=True)
            (other / "log.yaml").write_text("- m: a\n")
            subprocess.run(["git", "add", "."], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=tmpdir, check=True, capture_output=True)

            # Memory unchanged; an unrelated file (log.yaml) is dirty.
            (other / "log.yaml").write_text("- m: a\n- m: b\n")

            with patch.dict(os.environ, {"LMER_WORK_REPO_PATH": tmpdir}):
                result = commit_work_path("git.example.com/grp/proj/memory")
            assert result == 0
            # No new commit should have been created.
            rc, out = run_git_command(["log", "--oneline"], tmp, check=False)
            assert out.strip().count("\n") == 0  # exactly one commit (init)

    def test_commit_work_changes_custom_message(self):
        """Test commit with custom message"""
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "init"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmpdir, check=True, capture_output=True)

            target_dir = Path(tmpdir) / "github.com" / "owner" / "repo" / "review" / "pr-123"
            target_dir.mkdir(parents=True)
            (target_dir / "log.yaml").write_text("- message: test\n")

            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
                "LMER_TASK_TARGET": "pr-123",
            }

            with patch.dict(os.environ, env_vars):
                with patch("work_repo.git_ops.run_git_command") as mock_git:
                    def git_side_effect(cmd, cwd, check=False):
                        if cmd == ["commit", "-m", "Custom message"]:
                            return (0, "")
                        elif cmd == ["status", "--porcelain", "--", "github.com/owner/repo/review/pr-123"]:
                            return (0, "M  github.com/owner/repo/review/pr-123/log.yaml\n")
                        return (0, "")

                    mock_git.side_effect = git_side_effect
                    result = commit_work_changes("Custom message")
                    # Verify custom message was used
                    commit_calls = [call for call in mock_git.call_args_list if call[0][0] == ["commit", "-m", "Custom message"]]
                    assert len(commit_calls) > 0
