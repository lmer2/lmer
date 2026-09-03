"""Pluggable logging architecture for work repository."""

import os
import threading
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Optional
import yaml

from .run_state import _backup_bad_state, run_dir
from .utils import redact_secrets, _collect_secret_values


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

    def _read_entries(self) -> list:
        """The entries already in the log, or raise rather than lose them.

        An APPEND has to read the file first, and what it does when that read
        fails decides whether the run's history survives. This used to answer
        an unparseable or non-list file with ``[]`` — and the write below then
        replaced the whole file with the single new entry, so one bad line
        anywhere in a run's worklog destroyed every entry before it, and
        ``work log`` printed its success tick over the top. Nothing warned:
        the reader (``cli.render_truncated_log``) prints raw lines and never
        parses, so the file looked fine until the next append silently emptied
        it.

        Corruption is therefore treated the way ``release.yaml`` treats it
        (:func:`work_repo.run_state._backup_bad_state`, spec §6): the bad bytes
        are moved aside as ``log.yaml.bad-<utc>`` and the error is raised.
        ``cmd_log`` reports it and exits 1, which is the outcome that matters —
        a failed append leaves the history intact and tells the operator, while
        a successful one that silently ate it does not.

        An OSError is deliberately NOT a backup: a file this process cannot
        read at this moment is not a file we know to be corrupt, and renaming
        it would turn a transient permission or IO problem into a moved log.
        It raises unbacked, and the entries stay where they are.

        An absent file and an empty one are both "no entries yet" — the
        ordinary first append, and not corruption.
        """
        if not self.log_file.exists():
            return []
        try:
            raw = self.log_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError(
                f"{self.log_file} could not be read ({exc}) — refusing to "
                "append, because appending would rewrite the file from an "
                "empty list and lose every entry already in it"
            ) from exc
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise _backup_bad_state(
                self.log_file, f"{self.log_file.name} could not be parsed ({exc})"
            )
        if data is None:
            return []
        if not isinstance(data, list):
            raise _backup_bad_state(
                self.log_file,
                f"{self.log_file.name} holds a {type(data).__name__}, not the "
                "list of entries a log is",
            )
        return data

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

        logs = self._read_entries()

        # Append new entry
        logs.append(entry)

        self._publish(logs)

    def _publish(self, logs: list) -> None:
        """Replace the log with *logs*, atomically — write a temp file beside
        it, flush it to disk, and rename it over the target.

        `open(path, "w")` truncates first and dumps second, and that gap is
        the same data loss one door over from the parse case above. A kill
        mid-dump, an ENOSPC, or a second writer leaves a PREFIX of the file —
        and `yaml.dump` emits one entry per block, so a prefix cut at an entry
        boundary is valid YAML and a valid list, merely shorter. Nothing can
        see that anything is wrong: :meth:`_read_entries` parses it happily,
        the next append republishes the truncated history, and no `.bad-` copy
        is left to recover from. That is strictly worse than the corruption
        this class already refuses, because it is invisible.

        `os.replace` is atomic on POSIX, so a reader either sees the whole old
        file or the whole new one and never a half-written one. The `fsync`
        before it is what makes that true across a power loss rather than only
        across a process death: without it the rename can reach disk before
        the bytes it points at.

        The temp name carries process and thread for the reason
        :func:`work_repo.run_state.write_state` gives — two writers sharing one
        temp path is worse than losing a write, because the second's truncation
        lands inside the first's file and the first then publishes the hole.
        `work log` is not single-writer in practice: several sessions of one
        run write this file.

        A failure here leaves the live log exactly as it was, and the temp file
        is cleaned up rather than left beside it looking like a record.
        """
        tmp = self.log_file.with_name(
            f".{self.log_file.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                yaml.dump(logs, fh, default_flow_style=False, sort_keys=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.log_file)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


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
        # The run dir is the single home for run output (issue #87 D4):
        # log.yaml lives at the run root, resolved rename-proof through the
        # run-state kernel.
        rdir = run_dir()
        if rdir is None:
            raise ValueError(
                "LMER_REPO_HOST and LMER_REPO_PROJECT must be set to determine log file path"
            )

        # Build path: {host}/{project}/runs/{slug}/log.yaml
        log_file = rdir / "log.yaml"

    # Check for logger type from environment
    logger_type = os.environ.get("WORK_LOGGER_TYPE", "yaml").lower()

    if logger_type == "yaml":
        return YamlFileLogger(log_file)
    else:
        raise ValueError(f"Unknown logger type: {logger_type}")
