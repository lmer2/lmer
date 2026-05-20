#!/usr/bin/env python3
"""Tests for work_repo secret redaction."""

import os
import tempfile
import yaml
from pathlib import Path
from unittest.mock import patch

import pytest

from work_repo.utils import redact_secrets, _collect_secret_values, _REDACTED
from work_repo.loggers import YamlFileLogger
from work_repo.cli import cmd_log, cmd_report


class TestCollectSecretValues:
    """Test _collect_secret_values helper."""

    def test_collects_token_vars(self):
        env = {"GITLAB_TOKEN": "super-secret-token-value"}
        with patch.dict(os.environ, env, clear=True):
            secrets = _collect_secret_values()
            assert "super-secret-token-value" in secrets

    def test_collects_host_specific_token_vars(self):
        env = {"GITLAB_TOKEN_git_example_com": "my-host-token-value-here"}
        with patch.dict(os.environ, env, clear=True):
            secrets = _collect_secret_values()
            assert "my-host-token-value-here" in secrets

    def test_collects_secret_vars(self):
        env = {"MY_SECRET": "a-very-secret-value"}
        with patch.dict(os.environ, env, clear=True):
            secrets = _collect_secret_values()
            assert "a-very-secret-value" in secrets

    def test_collects_password_vars(self):
        env = {"DB_PASSWORD": "database-password-123"}
        with patch.dict(os.environ, env, clear=True):
            secrets = _collect_secret_values()
            assert "database-password-123" in secrets

    def test_ignores_short_values(self):
        """Short values are skipped to avoid false-positive replacements."""
        env = {"MY_TOKEN": "short"}
        with patch.dict(os.environ, env, clear=True):
            secrets = _collect_secret_values()
            assert "short" not in secrets

    def test_ignores_non_secret_vars(self):
        env = {"HOME": "/home/user", "PATH": "/usr/bin"}
        with patch.dict(os.environ, env, clear=True):
            secrets = _collect_secret_values()
            assert len(secrets) == 0

    def test_sorted_longest_first(self):
        """Longer secrets should come first to prevent partial replacements."""
        env = {
            "TOKEN_A": "short-token",
            "TOKEN_B": "a-much-longer-token-value",
        }
        with patch.dict(os.environ, env, clear=True):
            secrets = _collect_secret_values()
            assert len(secrets) == 2
            assert len(secrets[0]) >= len(secrets[1])

    def test_empty_env(self):
        with patch.dict(os.environ, {}, clear=True):
            secrets = _collect_secret_values()
            assert secrets == []

    def test_case_insensitive_name_match(self):
        env = {"my_api_key": "lowercase-key-value"}
        with patch.dict(os.environ, env, clear=True):
            secrets = _collect_secret_values()
            assert "lowercase-key-value" in secrets


class TestRedactSecrets:
    """Test redact_secrets function."""

    def test_redacts_token_in_text(self):
        env = {"GITLAB_TOKEN": "glpat-abc123def456ghi789"}
        with patch.dict(os.environ, env, clear=True):
            result = redact_secrets("Found token glpat-abc123def456ghi789 in output")
            assert "glpat-abc123def456ghi789" not in result
            assert _REDACTED in result

    def test_preserves_text_without_secrets(self):
        env = {"GITLAB_TOKEN": "glpat-abc123def456ghi789"}
        with patch.dict(os.environ, env, clear=True):
            text = "This is a normal log message with no secrets"
            result = redact_secrets(text)
            assert result == text

    def test_redacts_multiple_secrets(self):
        env = {
            "GITLAB_TOKEN": "first-secret-token-value",
            "API_KEY_OTHER": "second-secret-key-value",
        }
        with patch.dict(os.environ, env, clear=True):
            text = "Token: first-secret-token-value, Key: second-secret-key-value"
            result = redact_secrets(text)
            assert "first-secret-token-value" not in result
            assert "second-secret-key-value" not in result
            assert result.count(_REDACTED) == 2

    def test_redacts_same_secret_multiple_times(self):
        env = {"GITLAB_TOKEN": "repeated-secret-value"}
        with patch.dict(os.environ, env, clear=True):
            text = "repeated-secret-value appears twice: repeated-secret-value"
            result = redact_secrets(text)
            assert "repeated-secret-value" not in result
            assert result.count(_REDACTED) == 2

    def test_handles_empty_string(self):
        result = redact_secrets("")
        assert result == ""

    def test_handles_none_like_empty(self):
        # redact_secrets returns the input for falsy values
        result = redact_secrets("")
        assert result == ""

    def test_prints_warning_on_redaction(self, capsys):
        env = {"GITLAB_TOKEN": "glpat-abc123def456ghi789"}
        with patch.dict(os.environ, env, clear=True):
            redact_secrets("Token is glpat-abc123def456ghi789")
            captured = capsys.readouterr()
            assert "secret value(s) detected and redacted" in captured.err

    def test_no_warning_when_no_redaction(self, capsys):
        env = {"GITLAB_TOKEN": "glpat-abc123def456ghi789"}
        with patch.dict(os.environ, env, clear=True):
            redact_secrets("Clean text with no secrets")
            captured = capsys.readouterr()
            assert captured.err == ""

    def test_accepts_precomputed_secret_values(self):
        """Can pass secret_values directly to avoid re-scanning env."""
        result = redact_secrets(
            "Contains my-precomputed-secret here",
            secret_values=["my-precomputed-secret"],
        )
        assert "my-precomputed-secret" not in result
        assert _REDACTED in result

    def test_no_secrets_in_env(self):
        with patch.dict(os.environ, {}, clear=True):
            text = "Nothing to redact here"
            result = redact_secrets(text)
            assert result == text

    def test_redacts_glpat_prefix_without_env_var(self):
        """glpat- tokens are redacted by prefix even without a matching env var."""
        with patch.dict(os.environ, {}, clear=True):
            token = "glpat-FbDkAZmWrHWWGn62MK8a5G86MQp1OjZnCA"
            text = f"Token: {token} was used"
            result = redact_secrets(text)
            assert token not in result
            assert _REDACTED in result

    def test_redacts_sk_prefix_without_env_var(self):
        """sk- tokens (LLM API keys) are redacted by prefix even without a matching env var."""
        with patch.dict(os.environ, {}, clear=True):
            token = "sk-proj-abc123def456ghi789jkl012"
            text = f"Using API key {token} for requests"
            result = redact_secrets(text)
            assert token not in result
            assert _REDACTED in result

    def test_ignores_short_prefix_match(self):
        """Prefix patterns require 20+ trailing chars to avoid false positives."""
        with patch.dict(os.environ, {}, clear=True):
            text = "The sk-short value should not match"
            result = redact_secrets(text)
            assert result == text


class TestYamlFileLoggerRedaction:
    """Test that YamlFileLogger redacts secrets from log entries."""

    def test_redacts_secret_from_message(self):
        env = {"GITLAB_TOKEN": "glpat-secret-token-12345"}
        with patch.dict(os.environ, env, clear=True):
            with tempfile.TemporaryDirectory() as tmpdir:
                log_file = Path(tmpdir) / "log.yaml"
                logger = YamlFileLogger(log_file)

                logger.log("Authenticated with glpat-secret-token-12345")

                with open(log_file, "r") as f:
                    logs = yaml.safe_load(f)
                    assert "glpat-secret-token-12345" not in logs[0]["message"]
                    assert _REDACTED in logs[0]["message"]

    def test_redacts_secret_from_metadata(self):
        env = {"GITLAB_TOKEN": "glpat-secret-token-12345"}
        with patch.dict(os.environ, env, clear=True):
            with tempfile.TemporaryDirectory() as tmpdir:
                log_file = Path(tmpdir) / "log.yaml"
                logger = YamlFileLogger(log_file)

                logger.log(
                    "Task completed successfully via logging",
                    {"auth_info": "Used token glpat-secret-token-12345"},
                )

                with open(log_file, "r") as f:
                    logs = yaml.safe_load(f)
                    assert "glpat-secret-token-12345" not in logs[0]["auth_info"]
                    assert _REDACTED in logs[0]["auth_info"]

    def test_clean_message_unchanged(self):
        with patch.dict(os.environ, {}, clear=True):
            with tempfile.TemporaryDirectory() as tmpdir:
                log_file = Path(tmpdir) / "log.yaml"
                logger = YamlFileLogger(log_file)

                logger.log("Normal log message no secrets")

                with open(log_file, "r") as f:
                    logs = yaml.safe_load(f)
                    assert logs[0]["message"] == "Normal log message no secrets"


class TestCmdLogRedaction:
    """Test that cmd_log redacts secrets from the confirmation output."""

    def test_confirmation_output_is_redacted(self, capsys):
        env = {
            "GITLAB_TOKEN": "glpat-secret-token-12345",
            "LMER_WORK_REPO_PATH": "/tmp/fake",
            "LMER_REPO_HOST": "github.com",
            "LMER_REPO_PROJECT": "owner/repo",
            "LMER_TASK": "review",
            "LMER_TASK_TARGET": "pr-123",
        }
        with patch.dict(os.environ, env, clear=True):
            with tempfile.TemporaryDirectory() as tmpdir:
                log_file = Path(tmpdir) / "log.yaml"
                from work_repo.loggers import YamlFileLogger
                logger = YamlFileLogger(log_file)

                with patch("work_repo.cli.get_logger", return_value=logger):
                    result = cmd_log(
                        "Used token glpat-secret-token-12345 to authenticate",
                        [],
                    )

                    assert result == 0
                    captured = capsys.readouterr()
                    # The confirmation print should not contain the secret
                    assert "glpat-secret-token-12345" not in captured.out
                    assert _REDACTED in captured.out


class TestCmdReportRedaction:
    """Test that cmd_report redacts secrets from report file content."""

    def test_report_file_content_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir) / "work"
            work_dir.mkdir()

            # Create a report file with a secret in it
            source_report = Path(tmpdir) / "report.md"
            source_report.write_text(
                "# Review Report\n\nAuthenticated using glpat-secret-token-12345\n"
            )

            env = {
                "GITLAB_TOKEN": "glpat-secret-token-12345",
                "LMER_WORK_REPO_PATH": str(work_dir),
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
                "LMER_TASK_TARGET": "pr-456",
            }
            with patch.dict(os.environ, env, clear=True):
                result = cmd_report(str(source_report))

                assert result == 0

                # Find the copied report file
                target_dir = (
                    work_dir / "github.com" / "owner" / "repo" / "review" / "pr-456"
                )
                report_files = list(target_dir.glob("*.md"))
                assert len(report_files) == 1

                content = report_files[0].read_text()
                assert "glpat-secret-token-12345" not in content
                assert _REDACTED in content

    def test_clean_report_unchanged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir) / "work"
            work_dir.mkdir()

            source_report = Path(tmpdir) / "report.md"
            original_content = "# Clean Report\n\nNo secrets here.\n"
            source_report.write_text(original_content)

            env = {
                "LMER_WORK_REPO_PATH": str(work_dir),
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
                "LMER_TASK_TARGET": "pr-789",
            }
            with patch.dict(os.environ, env, clear=True):
                result = cmd_report(str(source_report))
                assert result == 0

                target_dir = (
                    work_dir / "github.com" / "owner" / "repo" / "review" / "pr-789"
                )
                report_files = list(target_dir.glob("*.md"))
                assert len(report_files) == 1
                assert report_files[0].read_text() == original_content
