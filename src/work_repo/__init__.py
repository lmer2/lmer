"""Work Repository Management Library."""

from .loggers import Logger, YamlFileLogger, get_logger
from .info_reader import read_project_info
from .git_ops import commit_work_changes
from .utils import sanitize_task_target, redact_secrets

__version__ = "0.1.0"
__all__ = [
    "Logger",
    "YamlFileLogger",
    "get_logger",
    "read_project_info",
    "commit_work_changes",
    "sanitize_task_target",
    "redact_secrets",
]
