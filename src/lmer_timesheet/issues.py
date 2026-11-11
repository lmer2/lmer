"""A handler's issue map (spec D25 of issue #665).

``timesheet.d/.handlers/<handler>/issues.yaml`` maps an issue URL to the
handler's field values for it. Whoever runs the handler's sync keeps it; lmer
only reads it. Keys are spelled by
:func:`lmer_timesheet.entry.normalise_issue_url`.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from lmer_timesheet.entry import _load_yaml, _shown, normalise_issue_url
from lmer_timesheet.handlers import HANDLERS_DIR, HandlerDefinition

__all__ = [
    "ISSUE_MAP_NAME",
    "issue_map_path",
    "parse_issue_map",
    "load_issue_map",
]

ISSUE_MAP_NAME = "issues.yaml"


def issue_map_path(handler_name: str) -> PurePosixPath:
    """Where *handler_name*'s issue map lives, relative to the work-repo root."""
    return HANDLERS_DIR / handler_name / ISSUE_MAP_NAME


def parse_issue_map(
    text: str, handler: HandlerDefinition
) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Parse an issue map for *handler*. Returns ``(issues, problems)``.

    ``issues`` maps a normalised issue URL to its field values. A map that is
    not YAML, uses an alias or is not a mapping is malformed: ``({}, [problem])``.
    Otherwise a bad key, a repeat after normalising, a value that is not a
    mapping, an unknown field or a value failing its field's pattern is
    skipped with a problem, and the rest of the map still counts.
    """
    data, problem = _load_yaml(text)
    if problem is not None:
        return {}, [problem]
    if data is None:
        return {}, []
    if not isinstance(data, dict):
        return {}, [f"not a mapping, but {_shown(data)}"]
    issues: dict[str, dict[str, str]] = {}
    problems: list[str] = []
    for key, raw in data.items():
        try:
            url = normalise_issue_url(key)
        except ValueError as exc:
            problems.append(f"{_shown(key)}: {exc}")
            continue
        if url in issues:
            problems.append(f"{url}: a repeat of an earlier key")
            continue
        if not isinstance(raw, dict):
            problems.append(f"{url}: must be a mapping of field values, not {_shown(raw)}")
            continue
        values: dict[str, str] = {}
        for field_key, value in raw.items():
            where = f"{url}: {_shown(field_key)}"
            field = handler.get_field(field_key) if isinstance(field_key, str) else None
            if field is None:
                problems.append(f"{where}: not a field of {handler.name}")
                continue
            if value is None or value == "":
                continue
            if isinstance(value, (bool, dict, list)) or not isinstance(value, (str, int, float)):
                problems.append(f"{where}: must be a string, not {_shown(value)}")
                continue
            value = str(value)
            if field.pattern and re.fullmatch(field.pattern, value) is None:
                problems.append(f"{where}: {value!r} does not match {field.pattern}")
                continue
            values[field.key] = value
        issues[url] = values
    return issues, problems


def load_issue_map(
    root: Path, handler: HandlerDefinition
) -> tuple[dict[str, dict[str, str]], list[str]]:
    """*handler*'s issue map under *root* (a work-repo checkout).

    A missing map is ``({}, [])``. Problems are prefixed with the map's
    repo-relative path.
    """
    rel = issue_map_path(handler.name)
    path = Path(root) / rel
    if not path.is_file():
        return {}, []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {}, [f"{rel}: unreadable: {exc}"]
    issues, problems = parse_issue_map(text, handler)
    return issues, [f"{rel}: {problem}" for problem in problems]
