"""Utility functions for work repository."""

import os
import re
import sys
from pathlib import Path
from typing import Optional

# Minimum length for an env var value to be considered a secret worth redacting.
# Very short values (e.g., "yes", "dev", "true") stored in secret-named env vars
# would match common words in normal text, causing false-positive replacements.
# Real API tokens are typically 20+ characters; 8 is a conservative floor.
_MIN_SECRET_LENGTH = 8

# Env var name patterns that indicate sensitive values
_SECRET_NAME_PATTERNS = re.compile(
    r"(TOKEN|API_KEY|SECRET|PASSWORD)", re.IGNORECASE
)

# Known token value prefixes — match directly in text regardless of env var name.
# Each pattern captures the full token (prefix + sufficient trailing characters).
_SECRET_VALUE_PREFIX_RE = re.compile(
    r"(glpat-[A-Za-z0-9_\-.]{20,}|sk-[A-Za-z0-9_\-.]{20,})"
)

_REDACTED = "***REDACTED***"


def _collect_secret_values() -> list[str]:
    """
    Collect sensitive values from environment variables.

    Scans all env vars whose names match common secret patterns
    (TOKEN, API_KEY, SECRET, PASSWORD) and returns their values,
    sorted longest-first so longer tokens are replaced before
    any shorter substrings.

    Returns:
        List of secret values (longest first)
    """
    secrets = set()
    for name, value in os.environ.items():
        if _SECRET_NAME_PATTERNS.search(name) and len(value) >= _MIN_SECRET_LENGTH:
            secrets.add(value)
    # Sort longest-first to avoid partial replacements
    return sorted(secrets, key=len, reverse=True)


def redact_secrets(text: str, secret_values: Optional[list[str]] = None) -> str:
    """
    Replace any secret values found in text with a redaction marker.

    Scans the text for values of environment variables whose names match
    sensitive patterns (TOKEN, API_KEY, SECRET, PASSWORD). Any matches
    are replaced with ***REDACTED*** and a warning is printed to stderr.

    Args:
        text: The text to redact
        secret_values: Pre-collected secret values (if None, collected from env)

    Returns:
        Text with secrets replaced by ***REDACTED***
    """
    if not text:
        return text

    if secret_values is None:
        secret_values = _collect_secret_values()

    redacted = text
    found = False
    for secret in secret_values:
        if secret in redacted:
            redacted = redacted.replace(secret, _REDACTED)
            found = True

    # Also redact tokens matching known value prefixes (e.g. glpat-, sk-)
    if _SECRET_VALUE_PREFIX_RE.search(redacted):
        redacted = _SECRET_VALUE_PREFIX_RE.sub(_REDACTED, redacted)
        found = True

    if found:
        print(
            "⚠️  Warning: secret value(s) detected and redacted from output",
            file=sys.stderr,
        )

    return redacted


def sanitize_task_target(task_target: str) -> str:
    """
    Sanitize task target for use in filesystem paths.

    Handles various formats:
    - URLs: Extract meaningful identifier (MR/PR number, branch name, etc.)
    - Branch names: Use as-is with minimal sanitization
    - Commit SHAs: Use as-is

    Args:
        task_target: Task target string (URL, branch name, commit SHA, etc.)

    Returns:
        Sanitized string safe for filesystem use
    """
    if not task_target:
        return "default"

    # If it's a URL, try to extract meaningful parts
    if task_target.startswith("http://") or task_target.startswith("https://"):
        # GitLab MR format: .../-/merge_requests/123
        if "/-/merge_requests/" in task_target.lower():
            parts = task_target.split("/-/merge_requests/")
            if len(parts) > 1:
                mr_id = parts[-1].split("/")[0].split("?")[0]
                return f"mr-{mr_id}"
        # GitLab issue format: .../-/issues/456
        if "/-/issues/" in task_target.lower():
            parts = task_target.split("/-/issues/")
            if len(parts) > 1:
                issue_id = parts[-1].split("/")[0].split("?")[0]
                return f"issue-{issue_id}"
        # GitHub PR format: .../pull/123
        if "/pull/" in task_target.lower():
            parts = task_target.split("/pull/")
            if len(parts) > 1:
                pr_id = parts[-1].split("/")[0].split("?")[0]
                return f"pr-{pr_id}"
        # GitHub issue format: .../issues/456
        if "/issues/" in task_target.lower() and "/pull/" not in task_target.lower():
            parts = task_target.split("/issues/")
            if len(parts) > 1:
                issue_id = parts[-1].split("/")[0].split("?")[0]
                return f"issue-{issue_id}"
        # Fallback: use the last meaningful segment of the URL
        path_parts = [p for p in task_target.split("/") if p and p not in ("http:", "https:", "")]
        if path_parts:
            # Take last part, remove query params and fragments
            last_part = path_parts[-1].split("?")[0].split("#")[0]
            return last_part.replace(":", "-")

    # For branch names, commit SHAs, etc., sanitize but preserve structure
    # Replace problematic characters but keep alphanumeric, dashes, underscores, dots
    sanitized = "".join(c if c.isalnum() or c in "-_." else "-" for c in task_target)
    # Collapse multiple dashes
    while "--" in sanitized:
        sanitized = sanitized.replace("--", "-")
    # Remove leading/trailing dashes
    sanitized = sanitized.strip("-")

    return sanitized if sanitized else "default"


def _work_repo_base() -> Optional[Path]:
    """
    Return the {LMER_WORK_REPO_PATH}/{host}/{project} base path.

    Reads the env-var trio that every work-repo path is built from:
    ``LMER_WORK_REPO_PATH`` (defaults to ``/work``), ``LMER_REPO_HOST``, and
    ``LMER_REPO_PROJECT``. Returns ``None`` if either the host or project is
    unset, since no meaningful path can be assembled without them. Callers
    keep ownership of their own "missing env var" error UX.

    Returns:
        The base ``Path``, or ``None`` if host/project are not configured.
    """
    repo_host = os.environ.get("LMER_REPO_HOST")
    repo_project = os.environ.get("LMER_REPO_PROJECT")
    if not repo_host or not repo_project:
        return None

    work_repo_path = Path(os.environ.get("LMER_WORK_REPO_PATH", "/work"))
    return work_repo_path / repo_host / repo_project


def project_info_dir() -> Optional[Path]:
    """
    Return ``{LMER_WORK_REPO_PATH}/{host}/{project}/info``.

    This is the global, project-wide info directory. Returns ``None`` if
    ``LMER_REPO_HOST`` or ``LMER_REPO_PROJECT`` is unset.
    """
    base = _work_repo_base()
    if base is None:
        return None
    return base / "info"


def project_memory_dir() -> Optional[Path]:
    """
    Return ``{LMER_WORK_REPO_PATH}/{host}/{project}/memory``.

    This is the per-project directory where persisted agent memory is stored,
    shared across all task types and targets for the project. Returns ``None``
    if ``LMER_REPO_HOST`` or ``LMER_REPO_PROJECT`` is unset.
    """
    base = _work_repo_base()
    if base is None:
        return None
    return base / "memory"


def task_info_dir() -> Optional[Path]:
    """
    Return ``{LMER_WORK_REPO_PATH}/{host}/{project}/{task}/info``.

    This is the task-specific info directory. ``LMER_TASK`` defaults to
    ``"default"`` when unset. Returns ``None`` if ``LMER_REPO_HOST`` or
    ``LMER_REPO_PROJECT`` is unset.
    """
    base = _work_repo_base()
    if base is None:
        return None
    task_type = os.environ.get("LMER_TASK", "default")
    return base / task_type / "info"


def task_target_dir() -> Optional[Path]:
    """
    Return ``{LMER_WORK_REPO_PATH}/{host}/{project}/{task}/{task_target}``.

    The task target directory holds the log file and report files for the
    current run. ``LMER_TASK`` and ``LMER_TASK_TARGET`` default to
    ``"default"`` when unset; the task target is passed through
    :func:`sanitize_task_target` to match the on-disk directory naming.
    Returns ``None`` if ``LMER_REPO_HOST`` or ``LMER_REPO_PROJECT`` is unset.
    """
    base = _work_repo_base()
    if base is None:
        return None
    task_type = os.environ.get("LMER_TASK", "default")
    task_target = os.environ.get("LMER_TASK_TARGET", "default")
    safe_task_target = sanitize_task_target(task_target) if task_target else "default"
    return base / task_type / safe_task_target
