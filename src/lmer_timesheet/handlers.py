"""Tracking handler definitions (spec D19).

A handler other than the built-in ``timesheet`` is one YAML file at
``timesheet.d/.handlers/<name>.yaml`` in the work repo. It declares the extra
fields an entry needs before time can be tracked under it; lmer enforces them
(:func:`validate_fields`) without knowing what they mean. A field marked
``per_issue`` takes its value per issue (issue #665), never from the project's
last value; a handler's issue map lives in ``.handlers/<name>/``, so the
``*.yaml`` glob here never reads it as a handler.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

from lmer_timesheet.entry import DEFAULT_HANDLER, TIMESHEET_DIR, _load_yaml, _shown, is_slug

__all__ = [
    "HANDLERS_DIR",
    "HandlerField",
    "HandlerDefinition",
    "TIMESHEET",
    "parse_handler",
    "load_handlers",
    "validate_fields",
]

HANDLERS_DIR = PurePosixPath(TIMESHEET_DIR, ".handlers")

_HANDLER_KEYS = ("name", "label", "fields", "sync")
_FIELD_KEYS = ("key", "label", "required", "pattern", "hint", "link", "per_issue")
_FIELD_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True, kw_only=True)
class HandlerField:
    """One extra field a handler asks for. ``pattern`` must match the whole value;
    ``per_issue`` means its value follows the entry's issue, not the project."""

    key: str
    label: str
    required: bool = False
    pattern: str | None = None
    hint: str | None = None
    link: str | None = None
    per_issue: bool = False

    def link_for(self, value: str) -> str | None:
        """The ``link`` template with ``{value}`` filled in, or ``None``."""
        if not self.link or not value:
            return None
        return self.link.replace("{value}", quote(value, safe=""))


@dataclass(frozen=True, kw_only=True)
class HandlerDefinition:
    """A tracking handler. ``sync`` means a sync process writes a ledger (D20)."""

    name: str
    label: str
    fields: tuple[HandlerField, ...] = ()
    sync: bool = False

    def get_field(self, key: str) -> HandlerField | None:
        for item in self.fields:
            if item.key == key:
                return item
        return None


TIMESHEET = HandlerDefinition(name=DEFAULT_HANDLER, label="Timesheet")


def parse_handler(
    text: str, *, path=None
) -> tuple[HandlerDefinition | None, list[str]]:
    """Parse one handler definition. Returns ``(handler, problems)``.

    ``handler`` is ``None`` when the definition cannot be used: YAML that
    does not parse or uses an alias, a field without a key, a duplicate
    key, a pattern that is not a regex or a link that is not http(s).
    Unknown keys are reported but do not fail it.
    *path*, when given, names the file: a missing ``name`` defaults to its
    stem and a different ``name`` is refused.
    """
    data, problem = _load_yaml(text)
    if problem is not None:
        return None, [problem]
    if not isinstance(data, dict):
        return None, ["not a mapping"]

    problems: list[str] = []
    fatal: list[str] = []
    for key in data:
        if key not in _HANDLER_KEYS:
            problems.append(f"{key}: unknown key")

    stem = PurePosixPath(str(path)).stem if path is not None else None
    name = data.get("name", stem)
    if not is_slug(name):
        fatal.append(f"name: {_shown(name)} is not a handler name")
    elif stem is not None and name != stem:
        fatal.append(f"name: {name!r} does not match the file name {stem!r}")

    label = data.get("label") or name
    if not isinstance(label, str):
        fatal.append("label: must be a string")

    sync = data.get("sync", False)
    if not isinstance(sync, bool):
        fatal.append("sync: must be true or false")

    raw_fields = data.get("fields") or []
    handler_fields: list[HandlerField] = []
    if not isinstance(raw_fields, list):
        fatal.append("fields: must be a list")
        raw_fields = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_fields):
        where = f"fields[{index}]"
        if not isinstance(raw, dict):
            fatal.append(f"{where}: must be a mapping")
            continue
        for key in raw:
            if key not in _FIELD_KEYS:
                problems.append(f"{where}.{key}: unknown key")
        key = raw.get("key")
        if not isinstance(key, str) or _FIELD_KEY_RE.fullmatch(key) is None:
            fatal.append(f"{where}.key: {_shown(key)} is not a field key")
            continue
        where = f"fields.{key}"
        if key in seen:
            fatal.append(f"{where}: defined twice")
            continue
        seen.add(key)
        field_problems = _field_problems(where, raw)
        if field_problems:
            fatal.extend(field_problems)
            continue
        handler_fields.append(
            HandlerField(
                key=key,
                label=raw.get("label") or key,
                required=raw.get("required", False),
                pattern=raw.get("pattern"),
                hint=raw.get("hint"),
                link=raw.get("link"),
                per_issue=raw.get("per_issue", False),
            )
        )

    if fatal:
        return None, fatal + problems
    return (
        HandlerDefinition(
            name=name, label=label, fields=tuple(handler_fields), sync=sync
        ),
        problems,
    )


def _field_problems(where: str, raw: dict) -> list[str]:
    problems = []
    for key in ("label", "pattern", "hint", "link"):
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            problems.append(f"{where}.{key}: must be a string")
    for key in ("required", "per_issue"):
        if not isinstance(raw.get(key, False), bool):
            problems.append(f"{where}.{key}: must be true or false")
    pattern = raw.get("pattern")
    if isinstance(pattern, str):
        try:
            re.compile(pattern)
        except (re.error, OverflowError, RecursionError) as exc:
            problems.append(f"{where}.pattern: not a regex: {exc}")
    link = raw.get("link")
    if isinstance(link, str) and not is_http_url(link):
        problems.append(f"{where}.link: must be an http(s) URL template")
    return problems


def is_http_url(value: str) -> bool:
    """*value* parses as a URL whose scheme is ``http`` or ``https``."""
    try:
        return urlsplit(value).scheme in ("http", "https")
    except ValueError:
        return False


def load_handlers(root: Path) -> tuple[dict[str, HandlerDefinition], list[str]]:
    """Every handler defined under *root* (a work-repo checkout), plus ``timesheet``.

    Returns ``(handlers, problems)``. A broken file is skipped and reported,
    prefixed with its repo-relative path; a file named ``timesheet.yaml`` is
    refused, so the built-in can never be overridden. A missing
    ``.handlers/`` directory is not a problem.
    """
    handlers = {TIMESHEET.name: TIMESHEET}
    problems: list[str] = []
    directory = Path(root) / HANDLERS_DIR
    if not directory.is_dir():
        return handlers, problems
    for path in sorted(directory.glob("*.yaml")):
        rel = HANDLERS_DIR / path.name
        if path.stem == TIMESHEET.name:
            problems.append(f"{rel}: cannot override the built-in timesheet handler")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"{rel}: unreadable: {exc}")
            continue
        handler, found = parse_handler(text, path=rel)
        problems.extend(f"{rel}: {problem}" for problem in found)
        if handler is not None:
            handlers[handler.name] = handler
    return handlers, problems


def validate_fields(handler: HandlerDefinition, values: dict | None) -> list[str]:
    """Why *values* cannot be tracked under *handler*; empty when they can.

    Each message starts with the field key: a required field with no value,
    or a value that does not fully match the field's pattern.
    """
    values = values or {}
    problems = []
    for item in handler.fields:
        value = values.get(item.key)
        if value is None or value == "":
            if item.required:
                problems.append(f"{item.key}: required ({item.label})")
            continue
        if item.pattern and re.fullmatch(item.pattern, str(value)) is None:
            problems.append(f"{item.key}: {value!r} does not match {item.pattern}")
    return problems
