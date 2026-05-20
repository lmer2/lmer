#!/usr/bin/env python3
"""Tests for work_repo.loggers module"""

import os
import pytest
import tempfile
import yaml
from pathlib import Path
from unittest.mock import patch

from work_repo.loggers import Logger, YamlFileLogger, get_logger


class TestYamlFileLogger:
    """Test YAML file logger"""

    def test_log_creates_file(self):
        """Test logging creates log file if it doesn't exist"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "log.yaml"
            logger = YamlFileLogger(log_file)

            logger.log("Test message")

            assert log_file.exists()
            with open(log_file, "r") as f:
                logs = yaml.safe_load(f)
                assert len(logs) == 1
                assert logs[0]["message"] == "Test message"
                assert "timestamp" in logs[0]

    def test_log_appends_to_existing_file(self):
        """Test logging appends to existing log file"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "log.yaml"
            logger = YamlFileLogger(log_file)

            logger.log("First message")
            logger.log("Second message")

            with open(log_file, "r") as f:
                logs = yaml.safe_load(f)
                assert len(logs) == 2
                assert logs[0]["message"] == "First message"
                assert logs[1]["message"] == "Second message"

    def test_log_with_metadata(self):
        """Test logging with metadata"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "log.yaml"
            logger = YamlFileLogger(log_file)

            logger.log("Test message", {"status": "success", "duration": "5m"})

            with open(log_file, "r") as f:
                logs = yaml.safe_load(f)
                assert logs[0]["message"] == "Test message"
                assert logs[0]["status"] == "success"
                assert logs[0]["duration"] == "5m"

    def test_log_creates_parent_directories(self):
        """Test logging creates parent directories if needed"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "nested" / "path" / "log.yaml"
            logger = YamlFileLogger(log_file)

            logger.log("Test message")

            assert log_file.exists()
            assert log_file.parent.exists()

    def test_log_handles_corrupted_yaml(self):
        """Test logger handles corrupted YAML gracefully"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "log.yaml"
            # Create corrupted YAML file
            with open(log_file, "w") as f:
                f.write("invalid: yaml: content: [\n")

            logger = YamlFileLogger(log_file)
            logger.log("New message")

            # Should create new log file or handle gracefully
            with open(log_file, "r") as f:
                logs = yaml.safe_load(f)
                # Should have at least the new message
                assert isinstance(logs, list)
                assert len(logs) >= 1


class TestGetLogger:
    """Test get_logger function"""

    def test_get_logger_with_explicit_path(self):
        """Test get_logger with explicit log file path"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "log.yaml"
            logger = get_logger(log_file)

            assert isinstance(logger, YamlFileLogger)
            assert logger.log_file == log_file

    def test_get_logger_from_env_vars(self):
        """Test get_logger determines path from environment variables"""
        with tempfile.TemporaryDirectory() as tmpdir:
            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
                "LMER_TASK_TARGET": "pr-123",
            }

            with patch.dict(os.environ, env_vars):
                logger = get_logger()

                assert isinstance(logger, YamlFileLogger)
                expected_path = Path(tmpdir) / "github.com" / "owner" / "repo" / "review" / "pr-123" / "log.yaml"
                assert logger.log_file == expected_path

    def test_get_logger_missing_env_vars(self):
        """Test get_logger raises error when env vars are missing"""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="LMER_REPO_HOST"):
                get_logger()

    def test_get_logger_unknown_type(self):
        """Test get_logger with unknown logger type"""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "log.yaml"
            with patch.dict(os.environ, {"WORK_LOGGER_TYPE": "unknown"}):
                with pytest.raises(ValueError, match="Unknown logger type"):
                    get_logger(log_file)
