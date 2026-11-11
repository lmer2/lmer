"""The entries view's join: one person's time, wherever it is (issue #604, spec D18).

An entry is in exactly one place at a time, and each place is a **stage**:

- ``running``: the person's live timer (:meth:`lmer_platform.timetrack.Tracker.status`);
- ``in outbox``: finished, waiting for the writer (:func:`lmer_platform.timesheet_store.pending`);
- ``kept locally``: finished but under the minimum length (D11);
- ``written``: a file under ``timesheet.d/<person>/``, read from the work-repo
  mirror **and** from the writer's clone (:func:`lmer_platform.timesheet_writer.written_entries`),
  so an entry pushed since the mirror last pulled is still listed. The mirror's
  copy wins when both have it, and the clone is not read at all once the mirror
  has pulled past what the clone last saw upstream
  (:func:`lmer_platform.timesheet_writer.upstream_ahead_of`): an entry deleted
  or moved upstream since must not come back from the stale clone.

A written entry under a ``sync: true`` handler also carries its ledger status
(D20): ``synced`` with the link, ``held`` with the reason, or ``pending`` when
the ledger has no line for it or the file changed since the line was written.

Every row has the same keys (``None`` where a key does not apply), so the
view renders one shape. ``raw`` is the entry as text: a written file's bytes as
stored, an outbox row's queued YAML, a running or kept entry's values as YAML.
``sessions`` is the runs the entry covered, ``{"run", "uid"}`` items (issue
#648), empty when it names none. Grouping and totals are the view's.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

import yaml

from lmer_timesheet import (
    TIMESHEET_DIR, HandlerDefinition, entry_hash, format_timestamp, is_person_key,
    ledger_path, parse_entry, parse_ledger, parse_timestamp, sync_status, validate_fields,
)

from . import projects, timesheet_handlers, timesheet_store, timesheet_writer
from .config import PlatformConfig

__all__ = [
    "STAGE_RUNNING", "STAGE_OUTBOX", "STAGE_KEPT", "STAGE_WRITTEN", "STAGES",
    "SYNC_SHOWN", "STATUSES", "Filters", "entries", "held",
]

STAGE_RUNNING = "running"
STAGE_OUTBOX = "in outbox"
STAGE_KEPT = "kept locally"
STAGE_WRITTEN = "written"
STAGES = (STAGE_RUNNING, STAGE_OUTBOX, STAGE_KEPT, STAGE_WRITTEN)
SYNC_SHOWN = ("synced", "held", "pending")
#: What ``status=`` may name: a stage or a sync status.
STATUSES = STAGES + SYNC_SHOWN

ORIGIN_TRACKER = "tracker"
ORIGIN_OUTBOX = "outbox"
ORIGIN_MIRROR = "mirror"
ORIGIN_CLONE = "clone"

#: ``<start>--<project-key>--<id>.yaml``, the D15 file name.
_FILE_NAME_RE = re.compile(
    r"^(?P<start>\d{8}T\d{6}Z)--(?P<project>[^/]+?)--(?P<id>[A-Za-z0-9][A-Za-z0-9._-]*)\.yaml$"
)


@dataclass(frozen=True)
class Filters:
    """The D18 filters. Dates bound ``start``; ``end`` is exclusive."""

    start: Optional[datetime] = None
    end: Optional[datetime] = None
    project: Optional[str] = None
    client: Optional[str] = None
    handler: Optional[str] = None
    status: Optional[str] = None

    @classmethod
    def parse(cls, *, start=None, end=None, project=None, client=None, handler=None,
              status=None) -> "Filters":
        """Filters from query text; ``ValueError`` names the one that cannot be used.

        ``start``/``end`` are a ``YYYY-MM-DD`` UTC day (``end`` includes that
        whole day) or an ISO 8601 timestamp with an offset. ``status`` is a
        stage or a sync status; ``in_outbox`` spells ``in outbox``.
        """
        status = _blank(status)
        if status is not None:
            status = status.replace("_", " ")
            if status not in STATUSES:
                raise ValueError(f"status {status!r} is not one of {', '.join(STATUSES)}")
        return cls(
            start=_bound(start, "from", end_of_day=False),
            end=_bound(end, "to", end_of_day=True),
            project=_blank(project), client=_blank(client), handler=_blank(handler),
            status=status,
        )

    def to_dict(self) -> dict:
        return {
            "from": format_timestamp(self.start) if self.start else None,
            "to": format_timestamp(self.end) if self.end else None,
            "project": self.project, "client": self.client,
            "handler": self.handler, "status": self.status,
        }

    def admits(self, row: dict) -> bool:
        begun = row["_start"]
        if self.start is not None and (begun is None or begun < self.start):
            return False
        if self.end is not None and (begun is None or begun >= self.end):
            return False
        if self.project is not None and self.project not in (row["project"], row["workspace_id"]):
            return False
        if self.client is not None and (row["client"] or "").casefold() != self.client.casefold():
            return False
        if self.handler is not None and row["handler"] != self.handler:
            return False
        if self.status in STAGES and row["stage"] != self.status:
            return False
        if self.status in SYNC_SHOWN and (row["sync"] or {}).get("status") != self.status:
            return False
        return True


def _blank(value) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _bound(value, name: str, *, end_of_day: bool) -> Optional[datetime]:
    value = _blank(value)
    if value is None:
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError:
        try:
            return parse_timestamp(value)
        except ValueError as exc:
            raise ValueError(f"{name}: {exc}; use YYYY-MM-DD or an ISO 8601 timestamp") from None
    moment = datetime.combine(day, time(), tzinfo=timezone.utc)
    return moment + timedelta(days=1) if end_of_day else moment


# --- the join ----------------------------------------------------------------

def entries(config: PlatformConfig, tracker, person: str, filters: Filters = Filters()) -> dict:
    """``{"person", "rows", "count", "filters", "problems"}`` for *person*.

    ``rows`` is sorted by start, newest first; rows whose start is unknown (a
    file too broken to parse, with no D15 name to read it from) come last.
    ``problems`` are the handler definitions' and the ledgers' own, prefixed
    with their paths; an entry's problems are on its row.
    """
    handlers, problems = timesheet_handlers.available(config)
    ledgers = _Ledgers(config.mirror_path, person)
    names = _project_names()
    written = _written_rows(config, person, handlers, ledgers, names)
    written_ids = {row["id"] for row in written}
    rows = _live_rows(tracker, person, handlers, names, skip_ids=written_ids) + written
    problems = list(problems) + ledgers.problems
    kept = [row for row in rows if filters.admits(row)]
    kept.sort(key=_newest_first)
    return {
        "person": person,
        "rows": [_public(row) for row in kept],
        "count": len(kept),
        "filters": filters.to_dict(),
        "problems": problems,
    }


def held(config: PlatformConfig, person: str) -> list:
    """*person*'s written entries a sync process holds, for the time status (D17).

    Only the mirror is read, and only the files the ledgers hold (by D15 file
    name; a file named otherwise is parsed to find out): a sync process can
    only have held an entry it read from the work repo.
    """
    handlers, _problems = timesheet_handlers.available(config)
    ledgers = _Ledgers(config.mirror_path, person)
    wanted = set()
    for handler in handlers.values():
        if handler.sync:
            wanted |= ledgers.held_ids(handler.name)
    if not wanted:
        return []
    names = _project_names()
    rows = []
    for path, raw in _mirror_files(config.mirror_path, person, ids=wanted):
        row = _file_row(path, raw, ORIGIN_MIRROR, handlers, ledgers, names)
        if (row["sync"] or {}).get("status") == "held":
            rows.append(row)
    rows.sort(key=_newest_first)
    return [_public(row) for row in rows]


def _newest_first(row: dict):
    begun = row["_start"]
    return (begun is None, -(begun.timestamp() if begun else 0.0))


def _public(row: dict) -> dict:
    return {key: value for key, value in row.items() if not key.startswith("_")}


def _project_names() -> dict:
    """``{project key: (workspace id, name)}`` for every project whose key is stored.

    Read, never derived: a project that never tracked time has no entries
    lmer wrote, and deriving its key here would store it from a GET.
    """
    names = {}
    for row in projects.listing():
        key = (row.get("settings") or {}).get("time_project_key")
        if key:
            names.setdefault(key, (row["id"], row["name"]))
    return names


def _row(*, stage: str, origin: str, id: Optional[str], person: str, project: Optional[str],
         names: dict, handlers: dict, handler: Optional[str], fields: Optional[dict], client="",
         title="", start: Optional[datetime], end: Optional[datetime], duration: Optional[int],
         trigger: Optional[dict], source: Optional[str], raw: str, path: Optional[str] = None,
         workspace_id: Optional[str] = None, project_name: Optional[str] = None,
         problems: Iterable = (), sync: Optional[dict] = None, mode: Optional[str] = None,
         outbox: Optional[dict] = None, kept: Optional[str] = None,
         sessions: Iterable = (), issues: Iterable = (), identity: Optional[dict] = None,
         title_source: Optional[str] = None) -> dict:
    known = names.get(project) if project else None
    definition = handlers.get(handler) if handler else None
    fields = dict(fields or {})
    links = {}
    if definition is not None:
        for key, value in fields.items():
            item = definition.get_field(key)
            link = item.link_for(value) if item is not None else None
            if link:
                links[key] = link
    return {
        "id": id,
        "stage": stage,
        "sync": sync,
        "person": person,
        "project": project,
        "workspace_id": workspace_id or (known[0] if known else None),
        "project_name": project_name or (known[1] if known else None),
        "handler": handler,
        "handler_label": definition.label if definition is not None else None,
        "fields": fields,
        "field_links": links,
        "client": client or "",
        "title": title or "",
        "start": format_timestamp(start) if start else None,
        "end": format_timestamp(end) if end else None,
        "duration": duration,
        "trigger": trigger,
        "source": source,
        "mode": mode,
        "path": path,
        "origin": origin,
        "raw": raw,
        "problems": list(problems),
        "outbox": outbox,
        "kept": kept,
        "sessions": [dict(item) for item in sessions],
        "issues": list(issues),
        "identity": identity,
        "title_source": title_source,
        "unassigned": _unassigned(definition, fields),
        "_start": start,
    }


def _unassigned(definition: Optional[HandlerDefinition], fields: dict) -> list:
    """The required ``per_issue`` fields *fields* leaves empty (#665 D25): missing, not broken."""
    if definition is None:
        return []
    return [item.key for item in definition.fields
            if item.per_issue and item.required and not fields.get(item.key)]


def _session_items(entry) -> list:
    return [ref.to_dict() for ref in entry.sessions]


def _as_yaml(values: dict) -> str:
    return yaml.safe_dump(values, sort_keys=False, allow_unicode=True, default_flow_style=False)


# --- live state ------------------------------------------------------------------

def _live_rows(tracker, person: str, handlers: dict, names: dict, *, skip_ids: set) -> list:
    """The running timer, the outbox and the kept slivers.

    An outbox row whose id is already written is the window between a push
    landing and its row leaving the outbox; the written row stands for it.
    """
    rows = []
    running = tracker.status(person).get("running") if tracker is not None else None
    if running:
        rows.append(_row(
            stage=STAGE_RUNNING, origin=ORIGIN_TRACKER, id=None, person=person,
            project=running["project_key"], workspace_id=running["workspace_id"],
            project_name=running.get("project_name"), names=names, handlers=handlers,
            handler=running["handler"], fields=running["fields"], client=running["client"],
            title=running["title"], start=parse_timestamp(running["start"]), end=None,
            duration=running.get("elapsed_seconds"),
            trigger={"start": running["start_trigger"], "stop": None}, source="lmer",
            mode=running["mode"], raw=_as_yaml(running), sessions=running.get("sessions") or (),
            issues=running.get("issues") or (), identity=running.get("identity"),
            title_source=running.get("title_source"),
        ))
    for item in timesheet_store.pending():
        entry, problems = parse_entry(item.entry_yaml, path=item.path)
        if entry is None or entry.person != person or entry.id in skip_ids:
            continue
        rows.append(_row(
            stage=STAGE_OUTBOX, origin=ORIGIN_OUTBOX, id=entry.id, person=person,
            project=entry.project, names=names, handlers=handlers, handler=entry.handler,
            fields=entry.fields, client=entry.client, title=entry.title, start=entry.start,
            end=entry.end, duration=entry.duration,
            trigger={"start": entry.trigger.start, "stop": entry.trigger.stop},
            source=entry.source, raw=item.entry_yaml, path=item.path, problems=problems,
            sessions=_session_items(entry), issues=entry.issues,
            outbox={"seq": item.seq, "state": item.state, "attempts": item.attempts,
                    "last_error": item.last_error, "enqueued_at": item.enqueued_at,
                    "last_attempt_at": item.last_attempt_at},
        ))
    for item in tracker.kept_entries(person) if tracker is not None else ():
        rows.append(_row(
            stage=STAGE_KEPT, origin=ORIGIN_TRACKER, id=item["id"], person=person,
            project=item["project"], workspace_id=item["workspace_id"], names=names,
            handlers=handlers, handler=item["handler"], fields=item["fields"],
            client=item["client"], title=item["title"], start=parse_timestamp(item["start"]),
            end=parse_timestamp(item["end"]), duration=item["duration"], trigger=item["trigger"],
            source="lmer", raw=_as_yaml(item), kept=item["reason"],
            sessions=item.get("sessions") or (), issues=item.get("issues") or (),
            identity=item.get("identity"),
        ))
    return rows


# --- written files and ledgers ------------------------------------------------

class _Ledgers:
    """*person*'s ledgers in the mirror, each read once, on first use."""

    def __init__(self, root: Path, person: str) -> None:
        self.root = Path(root)
        self.person = person
        self.problems: list = []
        self._lines: dict = {}

    def lines(self, handler: str) -> list:
        if handler not in self._lines:
            self._lines[handler] = self._read(handler)
        return self._lines[handler]

    def held_ids(self, handler: str) -> set:
        latest = {}
        for line in self.lines(handler):
            latest[line.id] = line.status
        return {entry_id for entry_id, status in latest.items() if status == "held"}

    def _read(self, handler: str) -> list:
        try:
            rel = ledger_path(self.person, handler)
        except ValueError:
            return []
        try:
            text = (self.root / rel).read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError) as exc:
            self.problems.append(f"{rel}: unreadable: {exc}")
            return []
        lines, problems = parse_ledger(text)
        self.problems.extend(f"{rel}: {problem}" for problem in problems)
        return lines


def _mirror_files(root: Path, person: str, *, ids: Optional[set] = None):
    """``(repo-relative path, bytes)`` of each ``.yaml`` under ``timesheet.d/<person>/``.

    Dot directories (the ledgers' ``.sync/``) are skipped. With *ids*, a file
    whose D15 name names another id is not read.
    """
    if not is_person_key(person):
        return
    base = Path(root) / TIMESHEET_DIR / person
    if not base.is_dir():
        return
    for directory, subdirs, files in os.walk(base):
        subdirs[:] = sorted(name for name in subdirs if not name.startswith("."))
        for name in sorted(files):
            if not name.endswith(".yaml") or name.startswith("."):
                continue
            if ids is not None:
                match = _FILE_NAME_RE.match(name)
                if match and match["id"] not in ids:
                    continue
            path = Path(directory) / name
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            yield path.relative_to(Path(root)).as_posix(), raw


def _written_rows(config: PlatformConfig, person: str, handlers: dict, ledgers: _Ledgers,
                  names: dict) -> list:
    rows = [_file_row(path, raw, ORIGIN_MIRROR, handlers, ledgers, names)
            for path, raw in _mirror_files(config.mirror_path, person)]
    paths = {row["path"] for row in rows}
    by_id: dict = {}
    for row in rows:
        by_id.setdefault(row["id"], []).append(row)
    for entry_id, same in by_id.items():
        if len(same) < 2:
            continue
        for row in same:
            others = ", ".join(other["path"] for other in same if other is not row)
            row["problems"].append(f"id: {entry_id!r} is also used by {others}")
    if not timesheet_writer.upstream_ahead_of(config.mirror_path):
        return rows
    for item in timesheet_writer.written_entries(person):
        if item.path in paths or item.id in by_id:
            continue
        rows.append(_file_row(item.path, item.raw, ORIGIN_CLONE, handlers, ledgers, names))
    return rows


def _file_row(path: str, raw: bytes, origin: str, handlers: dict, ledgers: _Ledgers,
              names: dict) -> dict:
    """One written file as a row, with its parse, field and ledger problems."""
    entry, problems = parse_entry(raw, path=path)
    text = raw.decode("utf-8", "replace")
    person = PurePosixPath(path).parts[1] if len(PurePosixPath(path).parts) > 1 else ""
    if entry is None:
        match = _FILE_NAME_RE.match(PurePosixPath(path).name)
        start = None
        if match:
            try:
                start = datetime.strptime(match["start"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                start = None
        return _row(
            stage=STAGE_WRITTEN, origin=origin,
            id=match["id"] if match else PurePosixPath(path).stem, person=person,
            project=match["project"] if match else None, names=names, handlers=handlers,
            handler=None, fields=None, start=start, end=None, duration=None, trigger=None,
            source=None, raw=text, path=path, problems=problems,
        )
    definition: Optional[HandlerDefinition] = handlers.get(entry.handler)
    if definition is None:
        problems.append(f"handler: {entry.handler!r} is not defined in the work repo")
    else:
        unassigned = set(_unassigned(definition, entry.fields))
        problems.extend(problem for problem in validate_fields(definition, entry.fields)
                        if problem.split(":", 1)[0] not in unassigned)
    sync = None
    if definition is not None and definition.sync:
        sync = _sync(entry, raw, ledgers.lines(definition.name))
    return _row(
        stage=STAGE_WRITTEN, origin=origin, id=entry.id, person=entry.person,
        project=entry.project, names=names, handlers=handlers, handler=entry.handler,
        fields=entry.fields, client=entry.client, title=entry.title, start=entry.start,
        end=entry.end, duration=entry.duration,
        trigger={"start": entry.trigger.start, "stop": entry.trigger.stop},
        source=entry.source, raw=text, path=path, problems=problems, sync=sync,
        sessions=_session_items(entry), issues=entry.issues,
    )


def _sync(entry, raw: bytes, lines: list) -> dict:
    """``{"status", "detail", "link", "ref", "reason", "at"}`` from the ledger (D20).

    A pending entry whose last line is stale keeps that line's link, so the
    external entry an edit has outdated can still be opened.
    """
    status, line = sync_status(entry, lines, raw=raw)
    detail = None
    if status == "pending":
        detail = "no ledger line yet" if line is None else "changed since it was synced"
    return {
        "status": status,
        "detail": detail,
        "link": line.link if line else None,
        "ref": line.ref if line else None,
        "reason": line.reason if line and status == "held" else None,
        "at": format_timestamp(line.at) if line else None,
        "hash": entry_hash(raw),
    }
