"""The assistant's delegated authority over a worker (issue #594).

A project's ``supervisor_authority`` setting says how far the operator has
delegated to that project's assistant: ``none``, ``approvals`` or ``full``. The
daemon resolves it per session and stamps it on every record the assistant's
credential posts, so a worker reads the platform's marking rather than a claim
in the message. Unset, missing or unreadable resolves to ``none``.

What each level permits, and the gates no level reaches, are in
``docs/PROJECTS.md``. Those are honoured by the worker; what the platform
enforces is the stamp itself and the bus's refusal of an assistant's answer to an
operator-only question (:mod:`lmer_cli.comms`).
"""
from __future__ import annotations

from typing import Optional

from lmer_cli.comms import AUTHORITY_NONE, SUPERVISOR_AUTHORITY_LEVELS

from . import projects, registry

SETTING_KEY = "supervisor_authority"
#: Exported to the worker at spawn: its level as it was at launch.
ENV = "LMER_SUPERVISOR_AUTHORITY"
#: The registry ``task`` field holding a per-spawn override: ``{"level", "capped"}``.
TASK_KEY = "supervisor_authority"

__all__ = [
    "SETTING_KEY", "ENV", "TASK_KEY", "AuthorityError", "validate", "project_level",
    "spawn_level", "for_session",
]


class AuthorityError(ValueError):
    """An unusable level, or an override its caller may not grant."""


def validate(value: object) -> str:
    if value not in SUPERVISOR_AUTHORITY_LEVELS:
        raise AuthorityError(
            f"supervisor_authority must be one of {', '.join(SUPERVISOR_AUTHORITY_LEVELS)}, got {value!r}"
        )
    return value


def _rank(level: str) -> int:
    return SUPERVISOR_AUTHORITY_LEVELS.index(level)


def project_level(workspace_id: Optional[str]) -> str:
    if not workspace_id or not projects.enabled():
        return AUTHORITY_NONE
    try:
        value = projects.get(workspace_id)["settings"].get(SETTING_KEY)
    except projects.ProjectError:
        return AUTHORITY_NONE
    return value if value in SUPERVISOR_AUTHORITY_LEVELS else AUTHORITY_NONE


def spawn_level(
    workspace_id: Optional[str], override: Optional[str], *, capped: bool,
) -> tuple[str, Optional[dict]]:
    """The level a new session launches with, and the override to record.

    A *capped* override (one the assistant asked for) may only narrow the
    project's level: the assistant cannot grant itself more than the operator
    delegated.
    """
    standing = project_level(workspace_id)
    if override is None:
        return standing, None
    level = validate(override)
    if capped and _rank(level) > _rank(standing):
        raise AuthorityError(
            f"supervisor_authority {level!r} is above this project's {standing!r}; "
            "only the operator may raise it"
        )
    return level, {"level": level, "capped": capped}


def for_session(session_id: str) -> str:
    """The level to stamp on the assistant's record to *session_id*, now.

    Resolved at send time, so a project setting changed mid-run applies to the
    next message. A capped override stays at or below the current project level.
    """
    standing = project_level(projects.session_home(session_id))
    entry = registry.read_session(session_id) or {}
    recorded = (entry.get("task") or {}).get(TASK_KEY)
    if not isinstance(recorded, dict) or recorded.get("level") not in SUPERVISOR_AUTHORITY_LEVELS:
        return standing
    level = recorded["level"]
    if recorded.get("capped") is not False and _rank(level) > _rank(standing):
        return standing
    return level
