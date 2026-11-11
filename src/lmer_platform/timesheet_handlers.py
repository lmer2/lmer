"""Which tracking handlers a project can choose (issue #604, spec D19).

The built-in ``timesheet`` is always available. Every other handler is a
definition in the work repo, read from the platform's mirror of it. A
project's ``time_handler`` is not checked against this list when it is set,
because the mirror may lag the work repo. While the name is not defined, or
its definition is unusable, no time starts on the project and the time status
says why. It never falls back to ``timesheet``: that handler's sync process
would never see the entries.

A handler's issue map (``.handlers/<name>/issues.yaml``, issue #665 D25) is read
from the same mirror. A broken map is listed with the handler problems but never
makes its handler unusable: its entries are skipped and the rest count.
"""
from __future__ import annotations

import threading
from typing import Optional

from lmer_timesheet import (
    HANDLERS_DIR, TIMESHEET, HandlerDefinition, issue_map_path, load_handlers, load_issue_map,
)

from .config import PlatformConfig

__all__ = ["available", "lookup", "resolve", "issue_map"]

#: ``{(mirror, handler): (stamp, definition, (map, problems))}``: the activity
#: path asks on every message, so a map is parsed again only when its file changes.
_MAP_CACHE: dict = {}
_MAP_LOCK = threading.Lock()


def available(config: PlatformConfig) -> tuple[dict[str, HandlerDefinition], list[str]]:
    """``(handlers by name, problems)`` from the work-repo mirror, ``timesheet`` included.

    The problems include each defined handler's issue-map problems.
    """
    handlers, problems = load_handlers(config.mirror_path)
    for name in sorted(handlers):
        if name != TIMESHEET.name:
            problems.extend(issue_map(config, handlers[name])[1])
    return handlers, problems


def issue_map(config: PlatformConfig, handler: HandlerDefinition) -> tuple[dict, list[str]]:
    """``(issue URL -> field values, problems)`` for *handler* from the mirror."""
    if handler.name == TIMESHEET.name:
        return {}, []
    path = config.mirror_path / issue_map_path(handler.name)
    try:
        info = path.stat()
        stamp = (info.st_mtime_ns, info.st_size, info.st_ino)
    except OSError:
        stamp = None
    key = (str(config.mirror_path), handler.name)
    with _MAP_LOCK:
        cached = _MAP_CACHE.get(key)
        if cached is not None and cached[0] == stamp and cached[1] == handler:
            return cached[2]
    result = ({}, []) if stamp is None else load_issue_map(config.mirror_path, handler)
    with _MAP_LOCK:
        _MAP_CACHE[key] = (stamp, handler, result)
    return result


def lookup(config: PlatformConfig, name: Optional[str]) -> tuple[Optional[HandlerDefinition], Optional[str]]:
    """The handler time on a project naming *name* is tracked under, or ``None`` and why.

    No name means the default, ``timesheet``.
    """
    if not name:
        return TIMESHEET, None
    handlers, problems = available(config)
    handler = handlers.get(name)
    if handler is not None:
        return handler, None
    own = [problem for problem in problems if problem.startswith(f"{HANDLERS_DIR / name}.yaml:")]
    if own:
        return None, (f"handler {name!r} is unusable ({'; '.join(own)});"
                      " time on this project does not start until it is fixed")
    return None, (f"handler {name!r} is not defined in the work repo;"
                  " time on this project does not start until it is")


def resolve(config: PlatformConfig, name: Optional[str]) -> tuple[HandlerDefinition, Optional[str]]:
    """What to show for a project naming *name*: :func:`lookup`'s handler and warning.

    While there is no handler, ``timesheet`` stands in so the project can still
    be rendered; the warning says nothing is tracked.
    """
    handler, warning = lookup(config, name)
    return handler or TIMESHEET, warning
