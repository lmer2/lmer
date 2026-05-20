"""Utility functions for work repository."""

import os
import re
import sys
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
