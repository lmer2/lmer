"""
Utility functions
"""

import os
import re
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


# Accepted truthy spellings for boolean values (compared lowercase). Shared
# by get_bool_env and lmer_cli.container.masterplan's task-manifest parsing so
# the accepted set cannot drift between the env toggle and the manifest.
TRUTHY_VALUES = frozenset({"1", "yes", "true"})
FALSY_VALUES = frozenset({"0", "no", "false"})


def get_bool_env(var_name: str, default: bool = False, *, env=None) -> bool:
    """
    Parse a boolean environment variable.

    Supports multiple formats:
    - Truthy: "1", "yes", "true", "True", "YES", "TRUE" (case-insensitive)
    - Falsy: "0", "no", "false", "False", "NO", "FALSE" (case-insensitive)
    - Empty/unset: returns default

    Args:
        var_name: Name of the environment variable
        default: Default value if variable is unset or empty
        env: Mapping to read instead of ``os.environ`` — for a caller that is
            handed an environment rather than running in it (a connector's
            config, a test)

    Returns:
        Boolean value parsed from environment variable
    """
    value = (env if env is not None else os.environ).get(var_name, "").strip().lower()
    if not value:
        return default

    if value in TRUTHY_VALUES:
        return True
    elif value in FALSY_VALUES:
        return False
    else:
        # Invalid value, return default
        return default


def decode_escape_bytes(value: str) -> bytes:
    """Decode a config/env string into raw bytes, unicode-escape style.

    The shared decode for byte-valued harness knobs — the supervisor's
    LMER_QUIT_SEQUENCE / LMER_AUTO_START_READY_MARKER env overrides and the
    user-harness manifest's supervisor fields (single source so the two
    encodings cannot drift): escapes spell out control bytes (``\\x03``),
    literal text passes through byte-for-byte (latin-1 round-trips raw UTF-8
    that came through unicode_escape as ≤U+00FF codepoints), and an explicit
    ``\\uXXXX`` escape above U+00FF lands as UTF-8 — what the TUI reads.

    Raises:
        UnicodeDecodeError: on an undecodable escape sequence.
    """
    decoded = value.encode("utf-8").decode("unicode_escape")
    try:
        return decoded.encode("latin-1")
    except UnicodeEncodeError:
        return decoded.encode("utf-8")


# Characters an identifier may contribute to an env-var name; everything else
# (dashes, dots, slashes) becomes an underscore.
_ENV_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9]")


def env_name_segment(text: str | None) -> str | None:
    """Return *text* as the segment an ``LMER_`` variable name can carry.

    The one home for that normalization, shared by the taskdef-scoped preset
    selector (``LMER_<TASK>_PRESET``) and the scoped resource limits
    (``LMER_<TASK>__<REPO>__MEMORY``) so the two conventions cannot drift:
    uppercased, with every non-alphanumeric collapsed to ``_`` and the result
    stripped of leading and trailing underscores.

    The mapping is deliberately **many-to-one** — ``code-review``,
    ``code_review`` and ``code.review`` all yield ``CODE_REVIEW``, as do
    ``agents/global`` and ``agents-global``. Callers name things that come from
    a filesystem or a forge, neither of which promises a spelling an env-var
    name could carry.

    Returns ``None`` when *text* is empty or normalizes to nothing — an id of
    only separators (``"-"``), or one with no ASCII alphanumerics at all
    (``"レビュー"``) — which leaves that caller with no scoped name rather than
    with a malformed one. A digit may lead the result: the ``LMER_`` prefix
    keeps the full name a legal identifier.
    """
    if not text or not text.strip():
        return None
    segment = _ENV_NAME_SAFE_RE.sub("_", text.strip()).strip("_").upper()
    return segment or None
