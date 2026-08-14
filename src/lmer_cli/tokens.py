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
import sys
from urllib.parse import urlparse

#: Refusal notices already printed by :func:`_get_gitlab_token`. One session
#: credentials many URLs against the same host, so without this the same
#: diagnostic would repeat once per lookup.
_warned: set[str] = set()


def _warn_once(message: str) -> None:
    """Print ``message`` to stderr the first time it is seen in this process."""
    if message in _warned:
        return
    _warned.add(message)
    print(message, file=sys.stderr)


def _host_from_git_url(url: str) -> str | None:
    """Extract the lowercased host from a git URL, or None if unparseable.

    Handles ``https://host/path``, ``https://user:pass@host/path`` (the form
    LMER_WORK_REPO takes once the host CLI has injected a token) and the
    scp-like ``git@host:path``. Deliberately hand-rolled rather than urlparse:
    the container copy in clone_and_exec.py must behave identically.
    """
    url = (url or "").strip()
    if not url:
        return None
    if "://" in url:
        authority = url.split("://", 1)[1].split("/", 1)[0]
    elif ":" in url:
        authority = url.split(":", 1)[0]
    else:
        return None
    if "@" in authority:
        authority = authority.rsplit("@", 1)[1]
    host = authority.split(":", 1)[0].strip().lower()
    return host or None


def _gitlab_token_issuing_host() -> str | None:
    """Host the generic ``GITLAB_TOKEN`` was issued for, or None if unknown.

    ``LMER_GITLAB_TOKEN_HOST`` names it explicitly; otherwise it defaults to
    the work-repo host, which is where a single-host setup's PAT comes from.
    """
    explicit = os.environ.get("LMER_GITLAB_TOKEN_HOST", "").strip()
    if explicit:
        return explicit.lower()
    return _host_from_git_url(os.environ.get("LMER_WORK_REPO", ""))


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
      5. ``GITLAB_TOKEN`` — generic fallback, but only for the host that
         issued it: ``LMER_GITLAB_TOKEN_HOST``, defaulting to the host in
         ``LMER_WORK_REPO``. Any other host (and any host at all when the
         issuing host is unknown) is refused with a one-time stderr notice,
         so a GitLab PAT is never sent to github.com or another third party
         (issue #161).

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

    token = os.environ.get("GITLAB_TOKEN")
    if not token:
        return None
    issuing_host = _gitlab_token_issuing_host()
    if issuing_host is None:
        _warn_once(
            f"⚠️  GITLAB_TOKEN not used for {host}: issuing host unknown — "
            f"set LMER_GITLAB_TOKEN_HOST or GITLAB_TOKEN_{suffix}"
        )
        return None
    if issuing_host != (host or "").lower():
        _warn_once(
            f"⚠️  GITLAB_TOKEN not used for {host}: issued for {issuing_host} — "
            f"set GITLAB_TOKEN_{suffix} for this host"
        )
        return None
    return token


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
