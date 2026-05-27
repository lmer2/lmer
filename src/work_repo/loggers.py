"""Pluggable logging architecture for work repository."""

import os
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Optional
import yaml

from .utils import redact_secrets, _collect_secret_values, task_target_dir


class Logger(ABC):
    """Abstract base class for loggers."""

    @abstractmethod
    def log(self, message: str, metadata: Optional[dict] = None) -> None:
        """
        Log a message.

        Args:
            message: The log message
            metadata: Optional metadata dictionary
        """
        pass


class YamlFileLogger(Logger):
    """YAML file logger implementation."""

    def __init__(self, log_file: Path):
        """
        Initialize YAML file logger.

        Args:
            log_file: Path to the log YAML file
        """
        self.log_file = log_file
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def log(self, message: str, metadata: Optional[dict] = None) -> None:
        """
        Append a log entry to the YAML file.

        Secret values from environment variables are automatically redacted.

        Args:
            message: The log message
            metadata: Optional metadata dictionary
        """
        # Collect secrets once and reuse for all redaction calls
        secret_values = _collect_secret_values()

        message = redact_secrets(message, secret_values)

        entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "message": message,
        }
        if metadata:
            redacted_metadata = {
                k: redact_secrets(str(v), secret_values) for k, v in metadata.items()
            }
            entry.update(redacted_metadata)

        # Read existing logs
        logs = []
        if self.log_file.exists():
            try:
                with open(self.log_file, "r", encoding="utf-8") as f:
                    logs = yaml.safe_load(f) or []
                    if not isinstance(logs, list):
                        logs = []
            except (yaml.YAMLError, IOError):
                logs = []

        # Append new entry
        logs.append(entry)

        # Write back
        with open(self.log_file, "w", encoding="utf-8") as f:
            yaml.dump(logs, f, default_flow_style=False, sort_keys=False)


def get_logger(log_file: Optional[Path] = None) -> Logger:
    """
    Get a logger instance based on configuration.

    Currently returns YAML file logger. Can be extended to support other types
    based on environment variables or config.

    Args:
        log_file: Path to log file (if None, will be determined from env vars)

    Returns:
        Logger instance
    """
    if log_file is None:
        # Determine log file path from environment variables
        tgt_dir = task_target_dir()

        if tgt_dir is None:
            raise ValueError(
                "LMER_REPO_HOST and LMER_REPO_PROJECT must be set to determine log file path"
            )

        # Build path: {host}/{project}/{task_type}/{task_target}/log.yaml
        log_file = tgt_dir / "log.yaml"

    # Check for logger type from environment
    logger_type = os.environ.get("WORK_LOGGER_TYPE", "yaml").lower()

    if logger_type == "yaml":
        return YamlFileLogger(log_file)
    else:
        raise ValueError(f"Unknown logger type: {logger_type}")
