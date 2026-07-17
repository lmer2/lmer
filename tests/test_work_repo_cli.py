#!/usr/bin/env python3
"""Tests for work_repo.cli module"""

import os
import pytest
import tempfile
import yaml
from pathlib import Path
from unittest.mock import patch, MagicMock
from io import StringIO
import sys

from work_repo.cli import cmd_commit, cmd_log, create_parser


class TestCmdLog:
    """Test cmd_log function"""

    def test_log_with_message(self):
        """Test logging with a message"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = (
                Path(tmpdir) / "github.com" / "owner" / "repo"
                / "runs" / "review-pr-123" / "log.yaml"
            )
            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
                "LMER_TASK_TARGET": "pr-123",
            }

            with patch.dict(os.environ, env_vars):
                # Mock get_logger to return a logger with our test log file
                from work_repo.loggers import YamlFileLogger
                logger = YamlFileLogger(log_file)

                with patch("work_repo.cli.get_logger", return_value=logger):
                    result = cmd_log("Test message with sufficient length", [])

                    assert result == 0
                    assert log_file.exists()
                    with open(log_file, "r") as f:
                        logs = yaml.safe_load(f)
                        assert len(logs) == 1
                        assert logs[0]["message"] == "Test message with sufficient length"

    def test_log_with_message_and_metadata(self):
        """Test logging with message and metadata"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = (
                Path(tmpdir) / "github.com" / "owner" / "repo"
                / "runs" / "review-pr-123" / "log.yaml"
            )
            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
                "LMER_TASK_TARGET": "pr-123",
            }

            with patch.dict(os.environ, env_vars):
                from work_repo.loggers import YamlFileLogger
                logger = YamlFileLogger(log_file)

                with patch("work_repo.cli.get_logger", return_value=logger):
                    result = cmd_log("Test message with sufficient length", ["key1=value1", "key2=value2"])

                    assert result == 0
                    with open(log_file, "r") as f:
                        logs = yaml.safe_load(f)
                        assert logs[0]["key1"] == "value1"
                        assert logs[0]["key2"] == "value2"

    def test_log_with_short_message(self):
        """Test logging with message that's too short"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = (
                Path(tmpdir) / "github.com" / "owner" / "repo"
                / "runs" / "review-pr-123" / "log.yaml"
            )
            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
                "LMER_TASK_TARGET": "pr-123",
            }

            with patch.dict(os.environ, env_vars):
                from work_repo.loggers import YamlFileLogger
                logger = YamlFileLogger(log_file)

                with patch("work_repo.cli.get_logger", return_value=logger):
                    captured_output = StringIO()
                    sys.stderr = captured_output
                    try:
                        result = cmd_log("short", [])
                        output = captured_output.getvalue()
                    finally:
                        sys.stderr = sys.__stderr__

                    assert result == 1
                    assert "too short" in output
                    assert "minimum 20 characters" in output
                    assert "Usage:" in output
                    assert "short" in output  # Should show the provided message
                    # Should not have logged anything (logger initialization might create empty file)
                    if log_file.exists():
                        logs = yaml.safe_load(log_file.read_text()) or []
                        assert len(logs) == 0

    def test_log_with_exactly_minimum_length(self):
        """Test logging with message that's exactly the minimum length"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = (
                Path(tmpdir) / "github.com" / "owner" / "repo"
                / "runs" / "review-pr-123" / "log.yaml"
            )
            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
                "LMER_TASK_TARGET": "pr-123",
            }

            with patch.dict(os.environ, env_vars):
                from work_repo.loggers import YamlFileLogger
                logger = YamlFileLogger(log_file)

                with patch("work_repo.cli.get_logger", return_value=logger):
                    # Exactly 20 characters
                    message = "a" * 20
                    result = cmd_log(message, [])

                    assert result == 0
                    assert log_file.exists()
                    with open(log_file, "r") as f:
                        logs = yaml.safe_load(f)
                        assert len(logs) == 1
                        assert logs[0]["message"] == message

    def test_log_without_message_displays_recent_entries(self):
        """Test logging without message displays last 50 lines"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = (
                Path(tmpdir) / "github.com" / "owner" / "repo"
                / "runs" / "review-pr-123" / "log.yaml"
            )
            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
                "LMER_TASK_TARGET": "pr-123",
            }

            # Create log file with 60 lines
            lines = [f"Line {i}\n" for i in range(60)]
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("".join(lines))

            with patch.dict(os.environ, env_vars):
                captured_output = StringIO()
                sys.stdout = captured_output
                try:
                    result = cmd_log(None, [])
                    output = captured_output.getvalue()
                finally:
                    sys.stdout = sys.__stdout__

                assert result == 0
                assert f"Log file: {log_file}" in output
                assert "(truncated to last 50 lines)" in output
                # Should show last 50 lines (lines 10-59)
                assert "Line 10" in output
                assert "Line 59" in output
                # Should not show first 10 lines
                assert "Line 0" not in output
                assert "Line 9" not in output

    def test_log_without_message_no_log_file(self):
        """Test logging without message when log file doesn't exist"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = (
                Path(tmpdir) / "github.com" / "owner" / "repo"
                / "runs" / "review-pr-123" / "log.yaml"
            )
            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
                "LMER_TASK_TARGET": "pr-123",
            }

            with patch.dict(os.environ, env_vars):
                captured_output = StringIO()
                sys.stdout = captured_output
                try:
                    result = cmd_log(None, [])
                    output = captured_output.getvalue()
                finally:
                    sys.stdout = sys.__stdout__

                assert result == 0
                assert f"Log file: {log_file}" in output
                assert "No log entries found." in output

    def test_log_without_message_empty_log_file(self):
        """Test logging without message when log file is empty"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = (
                Path(tmpdir) / "github.com" / "owner" / "repo"
                / "runs" / "review-pr-123" / "log.yaml"
            )
            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
                "LMER_TASK_TARGET": "pr-123",
            }

            # Create empty log file
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("")

            with patch.dict(os.environ, env_vars):
                captured_output = StringIO()
                sys.stdout = captured_output
                try:
                    result = cmd_log(None, [])
                    output = captured_output.getvalue()
                finally:
                    sys.stdout = sys.__stdout__

                assert result == 0
                assert f"Log file: {log_file}" in output
                assert "No log entries found." in output

    def test_log_without_message_less_than_50_lines(self):
        """Test logging without message when there are less than 50 lines"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = (
                Path(tmpdir) / "github.com" / "owner" / "repo"
                / "runs" / "review-pr-123" / "log.yaml"
            )
            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
                "LMER_TASK_TARGET": "pr-123",
            }

            # Create log file with 10 lines
            lines = [f"Line {i}\n" for i in range(10)]
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("".join(lines))

            with patch.dict(os.environ, env_vars):
                captured_output = StringIO()
                sys.stdout = captured_output
                try:
                    result = cmd_log(None, [])
                    output = captured_output.getvalue()
                finally:
                    sys.stdout = sys.__stdout__

                assert result == 0
                assert "(truncated" not in output  # Should not show truncation message
                # Should show all 10 lines
                for i in range(10):
                    assert f"Line {i}" in output


class TestCreateParser:
    """Test create_parser function"""

    def test_log_parser_message_optional(self):
        """Test that log parser has optional message argument"""
        parser = create_parser()

        # Parse with message
        args = parser.parse_args(["log", "test message"])
        assert args.command == "log"
        assert args.message == "test message"

        # Parse without message
        args = parser.parse_args(["log"])
        assert args.command == "log"
        assert args.message is None


class TestCmdCommit:
    """cmd_commit wiring for issue #85: it must run the stray-file reminder
    after committing, and never let the reminder alter the commit's exit code."""

    def test_reports_untracked_after_commit_and_passes_rc_through(self):
        """Commit runs first, then the reminder; the commit's rc is returned."""
        order = []
        with patch("work_repo.cli._sync_masterplan_links", return_value=[]), \
             patch(
                 "work_repo.cli.commit_work_changes",
                 side_effect=lambda m: order.append("commit") or 0,
             ) as mock_commit, \
             patch(
                 "work_repo.cli.report_uncommitted_work_items",
                 side_effect=lambda: order.append("report") or 3,
             ) as mock_report, \
             patch("work_repo.cli.commit_napkin_if_subdir", return_value=0), \
             patch("work_repo.cli.push_napkin_if_separate", return_value=0):
            rc = cmd_commit("my message")

        assert rc == 0
        mock_commit.assert_called_once_with("my message")
        mock_report.assert_called_once_with()
        # The reminder must fire AFTER the commit (so a just-committed run dir
        # is already clean and only genuine leftovers are flagged).
        assert order == ["commit", "report"]

    def test_reminder_findings_do_not_override_failed_commit_rc(self):
        """A non-zero commit rc stands even when the reminder finds items."""
        with patch("work_repo.cli._sync_masterplan_links", return_value=[]), \
             patch("work_repo.cli.commit_work_changes", return_value=1), \
             patch(
                 "work_repo.cli.report_uncommitted_work_items", return_value=5
             ) as mock_report, \
             patch("work_repo.cli.commit_napkin_if_subdir", return_value=0), \
             patch("work_repo.cli.push_napkin_if_separate", return_value=0):
            assert cmd_commit(None) == 1
        mock_report.assert_called_once_with()


class TestCmdCommitNapkin:
    def test_napkin_push_failure_does_not_change_work_exit(self):
        with patch("work_repo.cli._sync_masterplan_links", return_value=[]), \
             patch("work_repo.cli.commit_work_changes", return_value=0) as mock_work, \
             patch("work_repo.cli.report_uncommitted_work_items", return_value=0), \
             patch("work_repo.cli.commit_napkin_if_subdir", return_value=0), \
             patch("work_repo.cli.push_napkin_if_separate", return_value=1) as mock_napkin:
            rc = cmd_commit("msg")
        assert rc == 0                 # work-repo result is preserved
        mock_napkin.assert_called_once_with("msg")

    def test_napkin_subdir_commit_runs_and_failure_does_not_change_work_exit(self):
        """work commit must attempt the subdir-mode napkin capture (the
        work-repo commit paths never include {work_repo}/napkin/), and a
        failure there stays a warning."""
        with patch("work_repo.cli._sync_masterplan_links", return_value=[]), \
             patch("work_repo.cli.commit_work_changes", return_value=0), \
             patch("work_repo.cli.commit_napkin_if_subdir", return_value=1) as mock_subdir, \
             patch("work_repo.cli.push_napkin_if_separate", return_value=0):
            rc = cmd_commit("msg")
        assert rc == 0
        mock_subdir.assert_called_once_with("msg")
