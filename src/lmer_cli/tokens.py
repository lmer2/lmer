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


def _is_github_host(host: str) -> bool:
    """Return True for hosts that should consult GitHub-style token env vars.

    Covers public GitHub plus GitHub Enterprise Cloud subdomains. GitHub
    Enterprise Server can sit on any custom hostname and is not auto-detected;
    those users should set a per-host ``GITLAB_TOKEN_<sanitized_host>`` (the
    name is provider-agnostic in practice — see _get_gitlab_token).
    """
    if not host:
        return False
    h = host.lower()
    return h == "github.com" or h.endswith(".github.com") or h.endswith(".ghe.com")


def _get_gitlab_token(
    host: str, *, for_work_repo: bool = False, dedicated_env: str | None = None
) -> str | None:
    """
    Look up an API token for ``host`` from environment variables.

    Despite the historical name, this function supports both GitLab and
    GitHub hosts. Lookup order:

    0. ``dedicated_env`` — when given, the named env var is checked first
       (used by napkin/taskdef: ``LMER_NAPKIN_TOKEN`` / ``LMER_TASKDEF_TOKEN``).

    For work-repo lookups (``for_work_repo=True``), check next:
      1. ``LMER_WORK_REPO_TOKEN`` — provider-agnostic dedicated work-repo token
      2. ``GITLAB_TOKEN_worklog`` — deprecated; kept as fallback

    Then, for any lookup:
      3. ``GITLAB_TOKEN_{sanitized_host}`` — host-specific token (provider
         doesn't matter; the name is historical)
      4. For github.com / *.github.com / *.ghe.com: ``GH_TOKEN``, then
         ``GITHUB_TOKEN``
      5. ``GITLAB_TOKEN`` — generic fallback

    Returns the token string, or ``None`` if nothing matches.
    """
    if dedicated_env:
        token = os.environ.get(dedicated_env)
        if token:
            return token

    if for_work_repo:
        token = os.environ.get("LMER_WORK_REPO_TOKEN")
        if token:
            return token
        token = os.environ.get("GITLAB_TOKEN_worklog")
        if token:
            return token

    suffix = _sanitize_hostname(host)
    token = os.environ.get(f"GITLAB_TOKEN_{suffix}")
    if token:
        return token

    if _is_github_host(host):
        for var in ("GH_TOKEN", "GITHUB_TOKEN"):
            token = os.environ.get(var)
            if token:
                return token

    return os.environ.get("GITLAB_TOKEN")


def _prefer_ssh() -> bool:
    """Check if SSH is preferred over token authentication."""
    return os.environ.get("REPO_AUTH_PREFER_SSH", "").lower() in ("1", "true", "yes")


def _convert_ssh_to_https_if_token_available(
    ssh_url: str, *, for_work_repo: bool = False, dedicated_env: str | None = None
) -> str:
    """
    Convert an SSH git URL to HTTPS with token auth if a token is available.

    Args:
        ssh_url: Git URL in SSH format (e.g., 'git@gitlab.example.com:group/project')
        for_work_repo: If True, check _worklog suffix first for token lookup
        dedicated_env: If given, the named env var is checked first for the token
            (used by napkin/taskdef: ``LMER_NAPKIN_TOKEN`` / ``LMER_TASKDEF_TOKEN``)

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
        token = _get_gitlab_token(host, for_work_repo=for_work_repo, dedicated_env=dedicated_env)
        if token:
            # Ensure path ends with .git
            if not path.endswith('.git'):
                path = f"{path}.git"
            return f"https://oauth2:{token}@{host}/{path}"

        return ssh_url
    except Exception:
        return ssh_url


def _inject_gitlab_token_if_available(url: str, *, dedicated_env: str | None = None) -> str:
    """
    Inject GitLab token into HTTPS URL if available and not already present.

    Works with both plain HTTPS URLs and SSH URLs:
    - HTTPS: https://gitlab.example.com/group/project.git -> https://oauth2:TOKEN@gitlab.example.com/group/project.git
    - SSH: git@gitlab.example.com:group/project -> https://oauth2:TOKEN@gitlab.example.com/group/project.git

    The token is consumed on the host and baked into the returned URL, so the
    URL is cloneable as-is inside the container (where the standalone clone
    script has no token argument). This is the single credentialing helper used
    for the target repo and the optional napkin/taskdef repos alike.

    Args:
        url: Git URL in HTTPS or SSH format
        dedicated_env: If given, the named env var is checked first for the token
            (used by napkin/taskdef: ``LMER_NAPKIN_TOKEN`` / ``LMER_TASKDEF_TOKEN``),
            falling back to the standard host-based lookup.

    Returns:
        URL with token injected if available, otherwise original URL
    """
    if not url:
        return url

    # Handle SSH URLs by converting to HTTPS with token
    if url.startswith("git@"):
        return _convert_ssh_to_https_if_token_available(url, dedicated_env=dedicated_env)

    # Only process HTTPS URLs
    if not url.startswith("https://"):
        return url

    try:
        parsed = urlparse(url)

        # Don't inject if credentials already present
        if parsed.username or parsed.password:
            return url

        # Check if we have a token for this host
        token = _get_gitlab_token(parsed.hostname, dedicated_env=dedicated_env)
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
