"""An explicit workspace binding, propagated across async request boundaries."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import re

from . import store

GENERAL = "general"
_CURRENT = ContextVar("lmer_workspace", default=None)


def current() -> str | None:
    return _CURRENT.get()


@contextmanager
def bound(workspace_id: str | None):
    if workspace_id is not None and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", workspace_id):
        raise ValueError("invalid project id")
    token = _CURRENT.set(workspace_id)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def assistant_dir():
    """General keeps the paths already mounted into its live incarnation."""
    workspace_id = current()
    root = store.platform_dir()
    return root if workspace_id in (None, GENERAL) else root / "projects" / workspace_id
