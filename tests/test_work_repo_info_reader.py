#!/usr/bin/env python3
"""Tests for work_repo.info_reader module"""

import os
import time
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch

from work_repo.info_reader import read_project_info


class TestReadProjectInfo:
    """Test read_project_info function"""

    def test_read_project_info_no_files(self):
        """Test reading when no info files exist"""
        with tempfile.TemporaryDirectory() as tmpdir:
            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
            }

            with patch.dict(os.environ, env_vars):
                result = read_project_info()
                assert "No info files found" in result

    def test_read_project_info_project_only(self):
        """Test reading project info files only"""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_info_dir = Path(tmpdir) / "github.com" / "owner" / "repo" / "info"
            project_info_dir.mkdir(parents=True)

            (project_info_dir / "project.md").write_text("# Project Info\n\nProject details here.")

            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
            }

            with patch.dict(os.environ, env_vars):
                result = read_project_info()
                assert "project.md" in result
                assert "Project Info" in result
                assert "Project details here" in result

    def test_read_project_info_task_only(self):
        """Test reading task-specific info files only"""
        with tempfile.TemporaryDirectory() as tmpdir:
            task_info_dir = Path(tmpdir) / "github.com" / "owner" / "repo" / "review" / "info"
            task_info_dir.mkdir(parents=True)

            (task_info_dir / "task.md").write_text("# Task Info\n\nTask details here.")

            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
            }

            with patch.dict(os.environ, env_vars):
                result = read_project_info()
                assert "task.md" in result
                assert "Task Info" in result
                assert "Task details here" in result

    def test_read_project_info_both(self):
        """Test reading both project and task info files"""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_info_dir = Path(tmpdir) / "github.com" / "owner" / "repo" / "info"
            task_info_dir = Path(tmpdir) / "github.com" / "owner" / "repo" / "review" / "info"
            project_info_dir.mkdir(parents=True)
            task_info_dir.mkdir(parents=True)

            (project_info_dir / "project.md").write_text("# Project Info\n\nProject details.")
            (task_info_dir / "task.md").write_text("# Task Info\n\nTask details.")

            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
            }

            with patch.dict(os.environ, env_vars):
                result = read_project_info()
                assert "project.md" in result
                assert "task.md" in result
                assert "Project details" in result
                assert "Task details" in result
                # Should have separator
                assert "---" in result

    def test_read_project_info_multiple_files(self):
        """Test reading multiple info files"""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_info_dir = Path(tmpdir) / "github.com" / "owner" / "repo" / "info"
            project_info_dir.mkdir(parents=True)

            (project_info_dir / "file1.md").write_text("# File 1")
            (project_info_dir / "file2.md").write_text("# File 2")

            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
            }

            with patch.dict(os.environ, env_vars):
                result = read_project_info()
                assert "file1.md" in result
                assert "file2.md" in result

    def test_read_project_info_missing_env_vars(self):
        """Test reading when env vars are missing"""
        with patch.dict(os.environ, {}, clear=True):
            result = read_project_info()
            assert "Error" in result
            assert "LMER_REPO_HOST" in result

    def test_read_project_info_ignores_non_md_files(self):
        """Test that non-.md files are ignored"""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_info_dir = Path(tmpdir) / "github.com" / "owner" / "repo" / "info"
            project_info_dir.mkdir(parents=True)

            (project_info_dir / "file.md").write_text("# MD File")
            (project_info_dir / "file.txt").write_text("Text file")

            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
            }

            with patch.dict(os.environ, env_vars):
                result = read_project_info()
                assert "file.md" in result
                assert "file.txt" not in result

    def test_read_project_info_with_log_file(self):
        """Test that log.yaml is reported when it exists"""
        with tempfile.TemporaryDirectory() as tmpdir:
            task_target_dir = Path(tmpdir) / "github.com" / "owner" / "repo" / "review" / "pr-123"
            task_target_dir.mkdir(parents=True)

            log_file = task_target_dir / "log.yaml"
            log_file.write_text("logs: []")

            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
                "LMER_TASK_TARGET": "pr-123",
            }

            with patch.dict(os.environ, env_vars):
                result = read_project_info()
                assert "Task Related Work Log File" in result
                assert "log.yaml" in result
                assert str(log_file.resolve()) in result

    def test_read_project_info_with_report_files(self):
        """Test that report files are listed when they exist"""
        with tempfile.TemporaryDirectory() as tmpdir:
            task_target_dir = Path(tmpdir) / "github.com" / "owner" / "repo" / "review" / "pr-123"
            task_target_dir.mkdir(parents=True)

            report1 = task_target_dir / "241215-14-30-45.md"
            report2 = task_target_dir / "241214-10-20-30.md"
            report1.write_text("# Report 1")
            report2.write_text("# Report 2")

            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
                "LMER_TASK_TARGET": "pr-123",
            }

            with patch.dict(os.environ, env_vars):
                result = read_project_info()
                assert "Task Related Report Files" in result
                assert "241215-14-30-45.md" in result
                assert "241214-10-20-30.md" in result
                assert str(report1.resolve()) in result
                assert str(report2.resolve()) in result

    def test_read_project_info_report_files_sorted_by_time(self):
        """Test that report files are sorted by modification time, most recent first"""
        with tempfile.TemporaryDirectory() as tmpdir:
            task_target_dir = Path(tmpdir) / "github.com" / "owner" / "repo" / "review" / "pr-123"
            task_target_dir.mkdir(parents=True)

            # Create files with different modification times
            report_old = task_target_dir / "old.md"
            report_old.write_text("# Old Report")
            # Set modification time to 10 seconds ago
            old_time = time.time() - 10
            os.utime(report_old, (old_time, old_time))

            report_new = task_target_dir / "new.md"
            report_new.write_text("# New Report")
            # Set modification time to now
            new_time = time.time()
            os.utime(report_new, (new_time, new_time))

            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
                "LMER_TASK_TARGET": "pr-123",
            }

            with patch.dict(os.environ, env_vars):
                result = read_project_info()
                assert "Task Related Report Files" in result
                # Find positions of the files in the result
                old_pos = result.find("old.md")
                new_pos = result.find("new.md")
                # New file should appear before old file (most recent first)
                assert new_pos < old_pos

    def test_read_project_info_with_log_and_reports(self):
        """Test that both log file and report files are shown when both exist"""
        with tempfile.TemporaryDirectory() as tmpdir:
            task_target_dir = Path(tmpdir) / "github.com" / "owner" / "repo" / "review" / "pr-123"
            task_target_dir.mkdir(parents=True)

            log_file = task_target_dir / "log.yaml"
            log_file.write_text("logs: []")

            report = task_target_dir / "241215-14-30-45.md"
            report.write_text("# Report")

            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
                "LMER_TASK_TARGET": "pr-123",
            }

            with patch.dict(os.environ, env_vars):
                result = read_project_info()
                assert "Task Related Work Log File" in result
                assert "Task Related Report Files" in result
                assert str(log_file.resolve()) in result
                assert str(report.resolve()) in result

    def test_read_project_info_no_log_or_reports(self):
        """Test that log and report sections are not shown when files don't exist"""
        with tempfile.TemporaryDirectory() as tmpdir:
            task_target_dir = Path(tmpdir) / "github.com" / "owner" / "repo" / "review" / "pr-123"
            task_target_dir.mkdir(parents=True)

            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
                "LMER_TASK_TARGET": "pr-123",
            }

            with patch.dict(os.environ, env_vars):
                result = read_project_info()
                assert "Task Related Work Log File" not in result
                assert "Task Related Report Files" not in result

    def test_read_project_info_ignores_hidden_report_files(self):
        """Test that hidden report files (starting with .) are ignored"""
        with tempfile.TemporaryDirectory() as tmpdir:
            task_target_dir = Path(tmpdir) / "github.com" / "owner" / "repo" / "review" / "pr-123"
            task_target_dir.mkdir(parents=True)

            visible_report = task_target_dir / "report.md"
            visible_report.write_text("# Visible Report")

            hidden_report = task_target_dir / ".hidden.md"
            hidden_report.write_text("# Hidden Report")

            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
                "LMER_TASK_TARGET": "pr-123",
            }

            with patch.dict(os.environ, env_vars):
                result = read_project_info()
                assert "Task Related Report Files" in result
                assert "report.md" in result
                assert ".hidden.md" not in result
