#!/usr/bin/env python3
"""Tests for work_repo.git_ops module"""

import os
import pytest
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

from work_repo.git_ops import run_git_command, commit_work_changes


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
                        elif cmd == ["add", "github.com/owner/repo/review/pr-123"]:
                            return (0, "")
                        elif cmd == ["status", "--porcelain"]:
                            return (0, "M  github.com/owner/repo/review/pr-123/log.yaml\n")
                        elif cmd == ["commit", "-m", "Update work repo: github.com/owner/repo/review/pr-123"]:
                            return (0, "")
                        elif cmd == ["push"]:
                            return (0, "")
                        return (0, "")

                    mock_git.side_effect = git_side_effect
                    result = commit_work_changes()
                    assert result == 0

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
                        elif cmd == ["status", "--porcelain"]:
                            return (0, "M  github.com/owner/repo/review/pr-123/log.yaml\n")
                        return (0, "")

                    mock_git.side_effect = git_side_effect
                    result = commit_work_changes("Custom message")
                    # Verify custom message was used
                    commit_calls = [call for call in mock_git.call_args_list if call[0][0] == ["commit", "-m", "Custom message"]]
                    assert len(commit_calls) > 0
