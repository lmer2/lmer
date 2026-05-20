"""
Shared GitLab token utilities.

Centralizes token lookup, SSH-to-HTTPS conversion, and token injection
so that both the host CLI (cli.py) and other modules use one implementation.

Note: container/clone_and_exec.py runs standalone inside the container and
cannot import from this package. It maintains its own copy of _get_gitlab_token.
When updating token logic here, keep that copy in sync.
"""

import os
import re
from urllib.parse import urlparse


def _sanitize_hostname(host: str) -> str:
    """Sanitize a hostname into a valid environment variable suffix.

    Converts the hostname to lowercase and replaces dots/hyphens with underscores.
    Example: 'git.example.com' -> 'git_example_com'
    """
    return re.sub(r"[.\-]", "_", host.lower())


def _get_gitlab_token(host: str, *, for_work_repo: bool = False) -> str | None:
    """
    Get GitLab API token for a given host from environment variables.

    Checks for host-specific tokens first (using sanitized hostname),
    then falls back to generic GITLAB_TOKEN.

    Token lookup uses sanitized hostname as suffix:
    - GITLAB_TOKEN_{sanitized_host} (e.g., GITLAB_TOKEN_git_example_com)

    For work repo clones, checks _worklog suffix first (highest priority):
    - GITLAB_TOKEN_worklog

    Args:
        host: GitLab host (e.g., 'git.example.com', 'gitlab.example.com')
        for_work_repo: If True, check _worklog suffix first (for persistent work repo)

    Returns:
        API token if found, None otherwise
    """
    # For work repo, check _worklog suffix first (highest priority)
    if for_work_repo:
        token = os.environ.get("GITLAB_TOKEN_worklog")
        if token:
            return token

    # Try host-specific keys using sanitized hostname
    suffix = _sanitize_hostname(host)
    token = os.environ.get(f"GITLAB_TOKEN_{suffix}")
    if token:
        return token

    # Fall back to generic token
    return os.environ.get("GITLAB_TOKEN")


def _prefer_ssh() -> bool:
    """Check if SSH is preferred over token authentication."""
    return os.environ.get("REPO_AUTH_PREFER_SSH", "").lower() in ("1", "true", "yes")


def _convert_ssh_to_https_if_token_available(ssh_url: str, *, for_work_repo: bool = False) -> str:
    """
    Convert an SSH git URL to HTTPS with token auth if a token is available.

    Args:
        ssh_url: Git URL in SSH format (e.g., 'git@gitlab.example.com:group/project')
        for_work_repo: If True, check _worklog suffix first for token lookup

    Returns:
        HTTPS URL with token if available, otherwise original SSH URL.
        If REPO_AUTH_PREFER_SSH is set, always returns original SSH URL.
    """
    if not ssh_url or not ssh_url.startswith("git@"):
        return ssh_url

    # If SSH is preferred, skip token conversion
    if _prefer_ssh():
        return ssh_url

    # Parse SSH URL: git@host:path
    try:
        # Split on @ and then on :
        after_at = ssh_url[4:]  # Remove 'git@'
        if ':' not in after_at:
            return ssh_url
        host, path = after_at.split(':', 1)

        # Check if we have a token for this host
        token = _get_gitlab_token(host, for_work_repo=for_work_repo)
        if token:
            # Ensure path ends with .git
            if not path.endswith('.git'):
                path = f"{path}.git"
            return f"https://oauth2:{token}@{host}/{path}"

        return ssh_url
    except Exception:
        return ssh_url


def _inject_gitlab_token_if_available(url: str) -> str:
    """
    Inject GitLab token into HTTPS URL if available and not already present.

    Works with both plain HTTPS URLs and SSH URLs:
    - HTTPS: https://gitlab.example.com/group/project.git -> https://oauth2:TOKEN@gitlab.example.com/group/project.git
    - SSH: git@gitlab.example.com:group/project -> https://oauth2:TOKEN@gitlab.example.com/group/project.git

    Args:
        url: Git URL in HTTPS or SSH format

    Returns:
        URL with token injected if available, otherwise original URL
    """
    if not url:
        return url

    # Handle SSH URLs by converting to HTTPS with token
    if url.startswith("git@"):
        return _convert_ssh_to_https_if_token_available(url)

    # Only process HTTPS URLs
    if not url.startswith("https://"):
        return url

    try:
        parsed = urlparse(url)

        # Don't inject if credentials already present
        if parsed.username or parsed.password:
            return url

        # Check if we have a token for this host
        token = _get_gitlab_token(parsed.hostname)
        if not token:
            return url

        # Rebuild URL with token
        # Ensure path ends with .git
        path = parsed.path
        if not path.endswith('.git'):
            path = f"{path}.git"

        return f"https://oauth2:{token}@{parsed.hostname}{path}"
    except Exception:
        return url
