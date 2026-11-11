"""Timesheet entries: the D15 format, the ``timesheet.d/`` layout and the id.

One finished entry is one YAML file at
``timesheet.d/<person>/<YYYY>/<MM>/<start>--<project>--<id>.yaml``, the month
being the UTC month of ``start``. lmer writes every key, in the D15 order; a
hand-written entry needs only ``id``, ``person``, ``project``, ``start`` and
``end`` or ``duration``. :func:`parse_entry` reads both kinds and returns the
problems it found, so a reader can flag a broken file instead of dropping it.

``sessions`` (issue #648) and ``issues`` (issue #665) are the optional keys
lmer leaves out when empty: the runs whose sessions the entry covered, each a
:class:`SessionRef`, and the issue URLs its run was about, the first being the
work key (:func:`normalise_issue_url`).
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import PurePosixPath
from urllib.parse import urlsplit, urlunsplit

import yaml

__all__ = [
    "TIMESHEET_DIR",
    "DEFAULT_HANDLER",
    "TRIGGERS",
    "SOURCES",
    "RESERVED_PERSON_KEYS",
    "Trigger",
    "SessionRef",
    "Entry",
    "is_slug",
    "is_person_key",
    "is_project_key",
    "is_entry_id",
    "parse_timestamp",
    "format_timestamp",
    "parse_entry",
    "entry_path",
    "new_entry_id",
    "normalise_issue_url",
    "issue_label",
]

TIMESHEET_DIR = "timesheet.d"
DEFAULT_HANDLER = "timesheet"
TRIGGERS = ("manual", "activity", "idle", "switch", "hand", "edit")
SOURCES = ("lmer", "hand")
#: Names that would make ``timesheet.d/<person>/`` look like a work-repo area
#: (``lmer_schema.registry``); ``_*`` and ``.*`` are refused as well.
RESERVED_PERSON_KEYS = frozenset({"runs", "info", "memory", "specs"})

#: D15 table order; ``to_yaml`` writes the keys in exactly this order.
ENTRY_KEYS = (
    "id",
    "person",
    "project",
    "handler",
    "fields",
    "client",
    "title",
    "start",
    "end",
    "duration",
    "trigger",
    "source",
    "sessions",
    "issues",
)

_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")
#: A platform run key, ``host/project/<uid or slug>``: the spelling of
#: ``lmer_cli.runref``, which this package does not import: it depends only
#: on the standard library and PyYAML.
_RUN_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*){2,}")
_UID_RE = re.compile(r"[0-9a-f]{32}")
_SESSION_KEYS = ("run", "uid")
_ENTRY_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_PATH_TS_FORMAT = "%Y%m%dT%H%M%SZ"
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
#: What a string field may be coerced from; ``bool`` is an ``int``.
_SCALARS = (str, int, float, date)
_WORK_ITEM_RE = re.compile(r"/-/work_items/(\d+)$")
#: Issue-URL path endings and the sigil :func:`issue_label` gives them.
_LABELS = (
    (re.compile(r"/-/(?:issues|work_items)/(\d+)$"), "#"),
    (re.compile(r"/-/merge_requests/(\d+)$"), "!"),
    (re.compile(r"/(?:issues|pull)/(\d+)$"), "#"),
)


def is_slug(value) -> bool:
    """*value* is a key that is safe as one path segment: ``[a-z0-9][a-z0-9._-]*``."""
    return isinstance(value, str) and _SLUG_RE.fullmatch(value) is not None


def is_person_key(value) -> bool:
    """*value* is a slug and not one of :data:`RESERVED_PERSON_KEYS`."""
    return (
        is_slug(value)
        and value not in RESERVED_PERSON_KEYS
        and not value.startswith(("_", "."))
    )


def is_project_key(value) -> bool:
    """*value* is a slug with no ``--``, so an entry's file name splits back unambiguously."""
    return is_slug(value) and "--" not in value


def is_entry_id(value) -> bool:
    """*value* can be the ``<id>`` segment of an entry file name."""
    return isinstance(value, str) and _ENTRY_ID_RE.fullmatch(value) is not None


def new_entry_id(*, at: datetime | None = None) -> str:
    """A ULID: 48-bit millisecond time then 80 random bits, Crockford base32.

    Ids from different milliseconds sort in time order; two ids within one
    millisecond are ordered at random.
    """
    if at is None:
        millis = time.time_ns() // 1_000_000
    else:
        millis = int(at.timestamp() * 1000)
    value = (millis & ((1 << 48) - 1)) << 80 | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))


@dataclass(kw_only=True)
class Trigger:
    """What started and what stopped an entry, each one of :data:`TRIGGERS`."""

    start: str
    stop: str


@dataclass(frozen=True, kw_only=True)
class SessionRef:
    """One run an entry covered: its platform run key and, for a schema-2
    run, its execution uid. At least one of the two is set."""

    run: str = ""
    uid: str = ""

    def __post_init__(self):
        if not self.run and not self.uid:
            raise ValueError("a session reference needs a run or a uid")
        if self.run and not _RUN_KEY_RE.fullmatch(self.run):
            raise ValueError(f"run {self.run!r} is not a host/project/run key")
        if self.uid and not _UID_RE.fullmatch(self.uid):
            raise ValueError(f"uid {self.uid!r} is not 32 lowercase hex")

    def to_dict(self) -> dict:
        """The set keys only, ``run`` first."""
        return {key: getattr(self, key) for key in _SESSION_KEYS if getattr(self, key)}


@dataclass(kw_only=True)
class Entry:
    """One finished time entry (spec D15).

    ``start`` and ``end`` are timezone-aware. Give ``end`` or ``duration``
    (seconds); the other is derived from ``start``.
    """

    id: str = field(default_factory=new_entry_id)
    person: str
    project: str
    handler: str = DEFAULT_HANDLER
    fields: dict[str, str] = field(default_factory=dict)
    client: str = ""
    title: str = ""
    start: datetime
    end: datetime | None = None
    duration: int | None = None
    trigger: Trigger
    source: str = "lmer"
    sessions: list[SessionRef] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.end is None and self.duration is None:
            raise ValueError("an entry needs end or duration")
        # In UTC: subtracting two times in one ZoneInfo zone ignores a DST change.
        start = self.start.astimezone(timezone.utc)
        if self.duration is None:
            self.duration = int((self.end.astimezone(timezone.utc) - start).total_seconds())
        elif self.end is None:
            self.end = start + timedelta(seconds=self.duration)

    def to_dict(self) -> dict:
        """The entry as plain data, keys in D15 order, timestamps as UTC strings.

        ``sessions`` and ``issues`` are left out when empty, so an entry
        without them reads the same to a parser that predates the keys.
        """
        data = {
            "id": self.id,
            "person": self.person,
            "project": self.project,
            "handler": self.handler,
            "fields": dict(self.fields),
            "client": self.client,
            "title": self.title,
            "start": format_timestamp(self.start),
            "end": format_timestamp(self.end),
            "duration": self.duration,
            "trigger": {"start": self.trigger.start, "stop": self.trigger.stop},
            "source": self.source,
        }
        if self.sessions:
            data["sessions"] = [ref.to_dict() for ref in self.sessions]
        if self.issues:
            data["issues"] = list(self.issues)
        return data

    def to_yaml(self) -> str:
        """The entry file's text, every key written in D15 order (``sessions`` and ``issues`` when set)."""
        return yaml.safe_dump(
            self.to_dict(),
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        )


def format_timestamp(value: datetime) -> str:
    """ISO 8601 in UTC with a ``Z`` suffix; sub-second precision only when set."""
    if value.tzinfo is None:
        raise ValueError(f"timestamp {value!r} has no UTC offset")
    utc = value.astimezone(timezone.utc)
    spec = "microseconds" if utc.microsecond else "seconds"
    return utc.replace(tzinfo=None).isoformat(timespec=spec) + "Z"


def parse_timestamp(value) -> datetime:
    """An aware datetime from an ISO 8601 string (or a YAML-parsed datetime).

    Raises ``ValueError`` when the value is not a timestamp, has no offset or
    falls outside the years 1–9999 in UTC.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        raise ValueError(f"{value.isoformat()} is a date, not a timestamp")
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            raise ValueError(f"{value!r} is not an ISO 8601 timestamp") from None
    else:
        raise ValueError(f"{_shown(value)} is not an ISO 8601 timestamp")
    if parsed.tzinfo is None:
        raise ValueError(f"{value!r} has no UTC offset")
    try:
        parsed.astimezone(timezone.utc)
    except OverflowError:
        raise ValueError(f"{value!r} is out of range in UTC") from None
    return parsed


def _shown(value) -> str:
    """*value* for a problem message: its repr when a scalar, else its type.

    A YAML alias can make a list or mapping of any size from a few bytes, so
    those are never rendered.
    """
    if value is None or isinstance(value, _SCALARS):
        return repr(value)
    return f"a {type(value).__name__}"


def entry_path(entry: Entry) -> PurePosixPath:
    """Where *entry* lives, relative to the work-repo root.

    Raises ``ValueError`` when the person, project or id could not be one
    path segment, so no entry can be written outside its person's directory.
    """
    if not is_person_key(entry.person):
        raise ValueError(f"person key {entry.person!r} is not a valid person key")
    if not is_project_key(entry.project):
        raise ValueError(f"project key {entry.project!r} is not a valid project key")
    if not is_entry_id(entry.id):
        raise ValueError(f"entry id {entry.id!r} cannot be used in a file name")
    start = entry.start.astimezone(timezone.utc)
    name = f"{start.strftime(_PATH_TS_FORMAT)}--{entry.project}--{entry.id}.yaml"
    return PurePosixPath(
        TIMESHEET_DIR, entry.person, f"{start.year:04d}", f"{start.month:02d}", name
    )


def parse_entry(
    text: str | bytes, *, path=None
) -> tuple[Entry | None, list[str]]:
    """Parse one entry file. Returns ``(entry, problems)``.

    ``entry`` is ``None`` when the file cannot be an entry: unreadable YAML
    (YAML aliases included), a required key missing or a value that cannot
    be used. Otherwise the
    entry comes back with any softer problems (an unknown key, an
    inconsistent duration, a file in the wrong directory) for the caller to
    flag. Missing ``handler`` means ``timesheet``, missing ``source`` means
    ``hand``, a missing ``trigger`` side means ``hand`` and missing
    ``sessions`` or ``issues`` means none. A ``sessions`` or ``issues`` item
    that cannot be used is a soft problem and is dropped, so one bad reference
    never loses the entry; ``issues`` are kept normalised.

    *path*, when given, is the file's location; its last four parts are
    checked against the entry's person and start month.
    """
    problems: list[str] = []
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError as exc:
            return None, [f"not UTF-8: {exc}"]
    data, problem = _load_yaml(text)
    if problem is not None:
        return None, [problem]
    if not isinstance(data, dict):
        return None, ["not a mapping"]

    for key in data:
        if key not in ENTRY_KEYS:
            problems.append(f"{key}: unknown key")

    fatal: list[str] = []
    values: dict = {}

    for key in ("id", "person", "project"):
        value = data.get(key)
        if value is None or value == "":
            fatal.append(f"{key}: required")
        elif not isinstance(value, str):
            fatal.append(f"{key}: must be a string")
        else:
            values[key] = value
    if "person" in values and not is_person_key(values["person"]):
        fatal.append(f"person: {values['person']!r} is not a valid person key")
    if "project" in values and not is_project_key(values["project"]):
        fatal.append(f"project: {values['project']!r} is not a valid project key")

    for key in ("start", "end"):
        if data.get(key) is None:
            continue
        try:
            values[key] = parse_timestamp(data[key])
        except ValueError as exc:
            fatal.append(f"{key}: {exc}")
    if data.get("start") is None:
        fatal.append("start: required")

    duration = data.get("duration")
    if duration is not None:
        if isinstance(duration, bool) or not isinstance(duration, int):
            fatal.append("duration: must be a whole number of seconds")
        elif duration < 0:
            fatal.append("duration: must not be negative")
        else:
            values["duration"] = duration
    if data.get("end") is None and duration is None:
        fatal.append("end or duration: required")

    if "start" in values and "end" not in values and "duration" in values:
        try:
            values["end"] = values["start"].astimezone(timezone.utc) + timedelta(
                seconds=values["duration"]
            )
        except OverflowError:
            fatal.append("duration: too large: the end would be past the year 9999")
    elif "start" in values and "end" in values:
        span = int(
            (
                values["end"].astimezone(timezone.utc)
                - values["start"].astimezone(timezone.utc)
            ).total_seconds()
        )
        if span < 0:
            fatal.append("end: before start")
        elif "duration" in values and values["duration"] != span:
            problems.append(
                f"duration: {values['duration']} does not match start and end ({span})"
            )

    handler = data.get("handler")
    if handler is None or handler == "":
        values["handler"] = DEFAULT_HANDLER
    elif not is_slug(handler):
        fatal.append(f"handler: {_shown(handler)} is not a handler name")
    else:
        values["handler"] = handler

    raw_fields = data.get("fields")
    entry_fields: dict[str, str] = {}
    if raw_fields is None:
        pass
    elif not isinstance(raw_fields, dict):
        fatal.append("fields: must be a mapping")
    else:
        for key, value in raw_fields.items():
            if value is None:
                continue
            if not isinstance(value, _SCALARS):
                problems.append(f"fields.{key}: must be a string, not {_shown(value)}")
                continue
            if not isinstance(value, str):
                problems.append(f"fields.{key}: must be a string (quote it)")
            entry_fields[str(key)] = str(value)
    values["fields"] = entry_fields

    for key in ("client", "title"):
        value = data.get(key)
        if value is None:
            values[key] = ""
        elif not isinstance(value, _SCALARS):
            problems.append(f"{key}: must be a string, not {_shown(value)}")
            values[key] = ""
        elif not isinstance(value, str):
            problems.append(f"{key}: must be a string")
            values[key] = str(value)
        else:
            values[key] = value

    raw_trigger = data.get("trigger")
    sides = {"start": "hand", "stop": "hand"}
    if raw_trigger is None:
        pass
    elif not isinstance(raw_trigger, dict):
        problems.append("trigger: must be a mapping with start and stop")
    else:
        for side in ("start", "stop"):
            value = raw_trigger.get(side)
            if value is None:
                continue
            if value not in TRIGGERS:
                problems.append(f"trigger.{side}: {_shown(value)} is not one of {TRIGGERS}")
            if isinstance(value, _SCALARS):
                sides[side] = str(value)
    values["trigger"] = Trigger(**sides)

    source = data.get("source")
    if source is None:
        values["source"] = "hand"
    else:
        if source not in SOURCES:
            problems.append(f"source: {_shown(source)} is not one of {SOURCES}")
        values["source"] = str(source) if isinstance(source, _SCALARS) else "hand"

    values["sessions"] = _parse_sessions(data.get("sessions"), problems)
    values["issues"] = _parse_issues(data.get("issues"), problems)

    if fatal:
        return None, fatal + problems

    entry = Entry(**values)
    if path is not None:
        problems.extend(_location_problems(entry, path))
    return entry, problems


def _parse_sessions(raw, problems: list) -> list[SessionRef]:
    """The usable ``sessions`` items, unique and in file order; the rest go to *problems*."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        problems.append(f"sessions: must be a list, not {_shown(raw)}")
        return []
    refs: list[SessionRef] = []
    for index, item in enumerate(raw):
        where = f"sessions[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{where}: must be a mapping with run or uid, not {_shown(item)}")
            continue
        for key in item:
            if key not in _SESSION_KEYS:
                problems.append(f"{where}.{key}: unknown key")
        values = {}
        for key in _SESSION_KEYS:
            value = item.get(key)
            if value is None or value == "":
                continue
            if not isinstance(value, str):
                problems.append(f"{where}.{key}: must be a string, not {_shown(value)}")
                continue
            values[key] = value
        try:
            ref = SessionRef(**values)
        except ValueError as exc:
            problems.append(f"{where}: {exc}")
            continue
        if ref in refs:
            problems.append(f"{where}: a repeat of an earlier item")
            continue
        refs.append(ref)
    return refs


def _parse_issues(raw, problems: list) -> list[str]:
    """The usable ``issues`` items, normalised, unique and in file order; the rest go to *problems*."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        problems.append(f"issues: must be a list, not {_shown(raw)}")
        return []
    urls: list[str] = []
    for index, item in enumerate(raw):
        where = f"issues[{index}]"
        try:
            url = normalise_issue_url(item)
        except ValueError as exc:
            problems.append(f"{where}: {exc}")
            continue
        if url in urls:
            problems.append(f"{where}: a repeat of an earlier item")
            continue
        urls.append(url)
    return urls


def normalise_issue_url(text) -> str:
    """*text* as the one spelling an issue URL is compared, stored and looked up by.

    The scheme and host are lower-cased; the query, the fragment and trailing
    slashes are dropped; GitLab's ``/-/work_items/<n>`` becomes
    ``/-/issues/<n>``. An MR URL stays an MR. Raises ``ValueError`` for
    anything that is not an http(s) URL with a host and a path, or that
    carries credentials.
    """
    if not isinstance(text, str):
        raise ValueError(f"{_shown(text)} is not an issue URL")
    shown = repr(text) if len(text) <= 120 else repr(text[:117]) + "..."
    value = text.strip()
    if not value or any(char.isspace() for char in value):
        raise ValueError(f"{shown} is not an issue URL")
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        raise ValueError(f"{shown} is not an issue URL") from None
    if parts.scheme.lower() not in ("http", "https") or not host:
        raise ValueError(f"{shown} is not an http(s) issue URL")
    if "@" in parts.netloc:
        raise ValueError("an issue URL must not carry credentials")
    path = parts.path.rstrip("/")
    if not path:
        raise ValueError(f"{shown} names no issue")
    path = _WORK_ITEM_RE.sub(r"/-/issues/\1", path)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def issue_label(url: str) -> str:
    """A short name for *url*: ``#<n>`` for an issue, work item or GitHub
    pull request, ``!<n>`` for a GitLab merge request, else the URL itself."""
    try:
        path = urlsplit(url).path.rstrip("/")
    except ValueError:
        return url
    for pattern, sigil in _LABELS:
        match = pattern.search(path)
        if match:
            return f"{sigil}{match.group(1)}"
    return url


class _NoAliasLoader(yaml.SafeLoader):
    """``SafeLoader`` that refuses aliases.

    A few hundred bytes of nested aliases expand to millions of nodes, and
    merge keys (``<<: [*a, *a]``) make PyYAML itself build them, so a single
    committed file could stall every reader. Timesheet files never need one.
    """

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise yaml.composer.ComposerError(
                None, None, "aliases are not allowed", self.peek_event().start_mark
            )
        return super().compose_node(parent, index)


def _load_yaml(text: str):
    """``(data, None)`` from YAML *text*, or ``(None, problem)``; never raises.

    Besides its own errors, PyYAML lets ``ValueError`` (a 13th month, an
    integer past Python's digit limit) and ``RecursionError`` (deep nesting)
    escape.
    """
    try:
        return yaml.load(text, Loader=_NoAliasLoader), None
    except (yaml.YAMLError, ValueError, OverflowError, RecursionError) as exc:
        return None, f"not valid YAML: {exc}"


def _location_problems(entry: Entry, path) -> list[str]:
    parts = PurePosixPath(str(path).replace(os.sep, "/")).parts
    if len(parts) < 4:
        return []
    person, year, month = parts[-4], parts[-3], parts[-2]
    start = entry.start.astimezone(timezone.utc)
    problems = []
    if person != entry.person:
        problems.append(
            f"person: {entry.person!r} but the file is under {person!r}"
        )
    if (year, month) != (f"{start.year:04d}", f"{start.month:02d}"):
        problems.append(
            f"start: UTC month is {start.year:04d}/{start.month:02d} "
            f"but the file is under {year}/{month}"
        )
    return problems
