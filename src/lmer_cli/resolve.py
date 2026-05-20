"""
Repository URL resolution and validation.

This module handles resolving repository targets from various sources:
- Explicit URLs (http, https, ssh, git protocols)
- Local filesystem paths (validated as git repositories)
- Implicit inference from current directory's git origin

It provides utilities for git command execution and repository validation.
"""

import os
import subprocess
from pathlib import Path
from typing import Optional, Tuple


class ResolveError(Exception):
    """Exception raised when repository resolution fails."""
    pass


def _git(*args: str, cwd: Optional[Path] = None) -> Tuple[int, str]:
    """
    Execute a git command and return its status and output.

    Args:
        *args: Git command arguments (excluding 'git' itself)
        cwd: Optional working directory for command execution

    Returns:
        Tuple of (return code, stdout/stderr output)
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        return proc.returncode, proc.stdout.strip() or proc.stderr.strip()
    except FileNotFoundError:
        return 127, "git not found"


def get_remote_url(cwd: Path, remote_name: Optional[str] = None) -> Tuple[Optional[str], list[str]]:
    """
    Get git remote URL from a repository.

    Args:
        cwd: Directory to check for git repository
        remote_name: Optional specific remote name to use

    Returns:
        Tuple of (remote URL or None, list of all available remotes)
    """
    rc, _ = _git("rev-parse", "--is-inside-work-tree", cwd=cwd)
    if rc != 0:
        return None, []

    # Get all remotes
    rc, remotes_output = _git("remote", cwd=cwd)
    if rc != 0 or not remotes_output:
        return None, []

    available_remotes = [r.strip() for r in remotes_output.split('\n') if r.strip()]
    if not available_remotes:
        return None, []

    # If specific remote requested, use it
    if remote_name:
        if remote_name not in available_remotes:
            return None, available_remotes
        rc, url = _git("remote", "get-url", remote_name, cwd=cwd)
        if rc != 0 or not url:
            return None, available_remotes
        return url, available_remotes

    # Try origin first
    if "origin" in available_remotes:
        rc, url = _git("remote", "get-url", "origin", cwd=cwd)
        if rc == 0 and url:
            return url, available_remotes

    # If only one remote, use it
    if len(available_remotes) == 1:
        rc, url = _git("remote", "get-url", available_remotes[0], cwd=cwd)
        if rc != 0 or not url:
            return None, available_remotes
        return url, available_remotes

    # Multiple remotes without origin - ambiguous
    return None, available_remotes


def infer_origin_from_cwd(cwd: Path, remote_name: Optional[str] = None) -> Optional[str]:
    """
    Infer git origin URL from current working directory.

    Args:
        cwd: Directory to check for git repository
        remote_name: Optional specific remote name to use

    Returns:
        Origin URL if found, None otherwise

    Raises:
        ResolveError: If multiple remotes exist and no remote name specified
    """
    url, available_remotes = get_remote_url(cwd, remote_name)

    if url:
        return url

    if len(available_remotes) > 1 and not remote_name:
        raise ResolveError(
            f"Multiple git remotes found ({', '.join(available_remotes)}). "
            f"Please specify one using --remote"
        )

    return None


def is_likely_url(target: str) -> bool:
    """
    Check if target string appears to be a URL.

    Args:
        target: String to check

    Returns:
        True if target starts with common URL protocol prefixes
    """
    return target.startswith(("http://", "https://", "ssh://", "git@", "file:///"))


def validate_local_repo(path: Path) -> None:
    """
    Validate that a local path is a git repository.

    Args:
        path: Path to validate

    Raises:
        ResolveError: If path doesn't exist or is not a git repository
    """
    if not path.exists():
        raise ResolveError(f"Local path does not exist: {path}")
    # Accept either a bare repo or a worktree with .git present
    if (path / ".git").exists():
        return
    rc, _ = _git("rev-parse", "--is-inside-work-tree", cwd=path)
    if rc != 0:
        raise ResolveError(f"Not a git repository: {path}")


def normalize_repo_url(target: str, cwd: Path, remote_name: Optional[str] = None) -> Tuple[str, Optional[Path]]:
    """
    Normalize repository target to URL and optional local path.

    Handles three cases:
    1. Empty target: Infer from current directory's git origin
    2. Local path: Validate and extract origin URL from the repo's git remote
    3. URL: Use as-is

    Args:
        target: Repository target (URL, path, or empty)
        cwd: Current working directory for inference
        remote_name: Optional specific remote name to use

    Returns:
        Tuple of (repository URL, local path if applicable)

    Raises:
        ResolveError: If target cannot be resolved or validated
    """
    if not target:
        origin = infer_origin_from_cwd(cwd, remote_name)
        if not origin:
            raise ResolveError(
                "No <target> provided and could not infer git origin from current directory"
            )
        return origin, None

    if is_likely_url(target):
        return target, None

    # Treat as local path - validate and extract origin URL
    p = (cwd / target).resolve() if not os.path.isabs(target) else Path(target).resolve()
    validate_local_repo(p)

    # Get origin URL from the local repo's git remote
    origin = infer_origin_from_cwd(p, remote_name)
    if not origin:
        raise ResolveError(f"No git remote found in local repository: {p}")

    return origin, p
