"""Utility functions for work repository."""

import os
import re
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse

# Minimum length for an env var value to be considered a secret worth redacting.
# Very short values (e.g., "yes", "dev", "true") stored in secret-named env vars
# would match common words in normal text, causing false-positive replacements.
# Real API tokens are typically 20+ characters; 8 is a conservative floor.
_MIN_SECRET_LENGTH = 8

# Env var name patterns that indicate sensitive values.
# Mirrors the set used by _redact_env_value in lmer_cli/cli.py; the bare KEY
# subsumes API_KEY (DEPLOY_KEY, SIGNING_KEY, ... all match). Short values are
# still filtered out by _MIN_SECRET_LENGTH, which is what keeps names like
# KEY=yes from turning common words into redactions.
_SECRET_NAME_PATTERNS = re.compile(
    r"(TOKEN|KEY|SECRET|PASSWORD|CREDENTIALS)", re.IGNORECASE
)

# Secret-named variables whose values are structurally locators rather than
# credentials. Without this exact-name exclusion, the ambient value sweep
# stamps the configured hostname out of every matching URL and emits a false
# leak warning. Known token shapes and URL userinfo remain independently
# redacted from text regardless of the variable that carried them.
_SECRET_VALUE_LOCATOR_NAMES = frozenset({"LMER_GITLAB_TOKEN_HOST"})

# Known token value prefixes — match directly in text regardless of env var name.
# Each pattern captures the full token (prefix + sufficient trailing characters).
# High-entropy tails keep the {20,} floor; the shorter {10,} families (Slack,
# GitLab runner/deploy tokens) are distinctive enough on their prefix alone.
_SECRET_VALUE_PREFIX_RE = re.compile(
    r"("
    r"glpat-[A-Za-z0-9_\-.]{20,}"  # GitLab personal access token
    r"|gl(?:rt|dt)-[A-Za-z0-9_\-.]{10,}"  # GitLab runner / deploy token
    r"|sk-[A-Za-z0-9_\-.]{20,}"  # LLM API key
    r"|github_pat_[A-Za-z0-9_]{20,}"  # GitHub fine-grained PAT
    r"|gh[pousr]_[A-Za-z0-9]{20,}"  # GitHub classic PAT / OAuth / app tokens
    r"|xox[bp]-[A-Za-z0-9-]{10,}"  # Slack bot / user token
    r"|xapp-[A-Za-z0-9-]{10,}"  # Slack app-level token
    r")"
)

# Backstop for credentials embedded in URLs (https://oauth2:<token>@host/path).
# Replaces the whole userinfo segment regardless of token shape. The userinfo
# cannot contain "/", whitespace or "@", so plain URLs and bare email addresses
# (no "://" before them) are left alone.
_URL_USERINFO_RE = re.compile(r"(://)[^/\s@]+(@)")

_REDACTED = "***REDACTED***"


def _collect_secret_values() -> list[str]:
    """
    Collect sensitive values from environment variables.

    Scans all env vars whose names match common secret patterns
    (TOKEN, KEY, SECRET, PASSWORD, CREDENTIALS), excluding known locator
    variables, and returns their values sorted longest-first so longer tokens
    are replaced before any shorter substrings.

    Returns:
        List of secret values (longest first)
    """
    secrets = set()
    for name, value in os.environ.items():
        if (
            name.upper() not in _SECRET_VALUE_LOCATOR_NAMES
            and _SECRET_NAME_PATTERNS.search(name)
            and len(value) >= _MIN_SECRET_LENGTH
        ):
            secrets.add(value)
    # Sort longest-first to avoid partial replacements
    return sorted(secrets, key=len, reverse=True)


def redact_secrets(text: str, secret_values: Optional[list[str]] = None) -> str:
    """
    Replace any secret values found in text with a redaction marker.

    Scans the text for values of environment variables whose names match
    sensitive patterns (TOKEN, KEY, SECRET, PASSWORD, CREDENTIALS), for
    values carrying a known token prefix, and for credentials embedded in
    URLs. Any matches are replaced with ***REDACTED*** and a warning is
    printed to stderr.

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

    # Also redact tokens matching known value prefixes (e.g. glpat-, ghp-, xoxb-)
    if _SECRET_VALUE_PREFIX_RE.search(redacted):
        redacted = _SECRET_VALUE_PREFIX_RE.sub(_REDACTED, redacted)
        found = True

    # Backstop: strip userinfo from URLs, whatever the credential looks like
    if _URL_USERINFO_RE.search(redacted):
        redacted = _URL_USERINFO_RE.sub(rf"\g<1>{_REDACTED}\g<2>", redacted)
        found = True

    if found:
        print(
            "⚠️  Warning: secret value(s) detected and redacted from output",
            file=sys.stderr,
        )

    return redacted


def is_secret_env_name(name: str) -> bool:
    """True if an env var name matches the sensitive-name pattern.

    The shared name rule (TOKEN/KEY/SECRET/PASSWORD/CREDENTIALS) behind the
    redaction sinks and the prompt/taskdef render-context filters, exposed so
    callers outside this module never grow their own copy of the regex.
    """
    return bool(_SECRET_NAME_PATTERNS.search(name))


def strip_url_credentials(url):
    """Strip any embedded ``user:password@`` credentials from a URL.

    Unlike :func:`redact_secrets`, which stamps a redaction marker into free
    text, this keeps the URL usable: scheme/host/port/path survive and only
    the userinfo is removed (``https://oauth2:tok@host/p`` →
    ``https://host/p``). Values that are not credentialed URLs pass through
    unchanged — including scp-style SSH remotes (``git@host:path``), whose
    userinfo is protocol plumbing, not a credential.
    """
    if not url or "://" not in url or "@" not in url:
        return url
    try:
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            return urlunparse(parsed._replace(netloc=netloc))
        return url
    except Exception:
        # Fail closed: never return a value that may still carry the
        # credential when parsing fails (e.g. an out-of-range port makes
        # `parsed.port` raise). Strip the userinfo with a regex that cannot
        # raise instead.
        return re.sub(r"(://)[^/]*@", r"\1", url)


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
        # GitLab work item format (newer GitLab UI): .../-/work_items/70
        # Work items are issues; normalize to the same issue-{id} form so a
        # work_items URL and its equivalent /-/issues/ URL share a target.
        if "/-/work_items/" in task_target.lower():
            parts = task_target.split("/-/work_items/")
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
