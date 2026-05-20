"""
Utility functions
"""

import os
import subprocess


def resolve_human_identity() -> str | None:
    """
    Resolve the host user's human identity for forwarding to the container.

    Resolution order:
    1. ``LMER_HUMAN_IDENTITY`` env var (explicit, free-form string)
    2. Host ``git config --get user.name`` and/or ``user.email`` (respects
       the normal precedence chain: system → global → local, including
       ``[include]`` directives and ``GIT_CONFIG_GLOBAL``)
    3. ``None`` if neither is available

    The returned string is intended to be shown to the model in the system
    prompt so it can attribute matching usernames/emails/handles in
    repository artifacts (PRs, MRs, issues, comments) to the user it is
    collaborating with, rather than asking them to confirm their identity.
    """
    explicit = os.environ.get("LMER_HUMAN_IDENTITY", "").strip()
    if explicit:
        return explicit

    name = _git_config("user.name")
    email = _git_config("user.email")

    if name and email:
        return f"{name} <{email}>"
    if name:
        return name
    if email:
        return email
    return None


def _git_config(key: str) -> str | None:
    """Return a value from the host's effective git config, or ``None``.

    Uses ``git config --get`` (no scope flag) so the normal precedence chain
    is respected: system → global → local, with ``[include]`` and
    ``GIT_CONFIG_GLOBAL`` honored.
    """
    try:
        result = subprocess.run(
            ["git", "config", "--get", key],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def get_bool_env(var_name: str, default: bool = False) -> bool:
    """
    Parse a boolean environment variable.

    Supports multiple formats:
    - Truthy: "1", "yes", "true", "True", "YES", "TRUE" (case-insensitive)
    - Falsy: "0", "no", "false", "False", "NO", "FALSE" (case-insensitive)
    - Empty/unset: returns default

    Args:
        var_name: Name of the environment variable
        default: Default value if variable is unset or empty

    Returns:
        Boolean value parsed from environment variable
    """
    value = os.environ.get(var_name, "").strip().lower()
    if not value:
        return default

    truthy_values = {"1", "yes", "true"}
    falsy_values = {"0", "no", "false"}

    if value in truthy_values:
        return True
    elif value in falsy_values:
        return False
    else:
        # Invalid value, return default
        return default
