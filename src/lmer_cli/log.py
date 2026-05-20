"""
Logging utilities for LMER CLI.

Provides simple logging functions with verbosity control via environment variable.
Messages are printed to stdout/stderr based on verbosity level and message type.
"""

import os


def is_verbose() -> bool:
    """
    Check if verbose logging is enabled.

    Returns:
        True if LMER_VERBOSE environment variable is set to truthy value
    """
    return os.environ.get("LMER_VERBOSE", "0") in ("1", "true", "True")


def info(msg: str) -> None:
    """
    Print informational message if verbose mode is enabled.

    Args:
        msg: Message to print
    """
    if is_verbose():
        print(msg)


def success(msg: str) -> None:
    """
    Print success message unconditionally.

    Always prints regardless of verbosity level, used for important
    status updates like SSH agent configuration.

    Args:
        msg: Success message to print
    """
    print(msg)


def error(msg: str) -> None:
    """
    Print error message unconditionally.

    Args:
        msg: Error message to print
    """
    print(msg)


def warning(msg: str) -> None:
    """
    Print warning message unconditionally.

    Args:
        msg: Warning message to print
    """
    print(msg)
