"""The time tracker: one running timer per person, and the D4 rules (issue #604).

Two calls are the whole contract every trigger goes through:
:func:`start_tracking_time` and :func:`stop_tracking_time`, acting on the
tracker the daemon registered (:func:`register_tracker`). :class:`Tracker`
adds what the activity consumer needs (:meth:`Tracker.note_activity`,
:meth:`Tracker.close_idle`) and what the time API shows and edits
(:meth:`Tracker.update_running`, :meth:`Tracker.status`).

- **Start changes only live state (D8).** A running timer is a row in
  :mod:`lmer_platform.timesheet_store`; values edited while it runs are kept
  in that row and land in the entry.
- **Stop builds one complete entry** and queues it for the writer in the same
  transaction that ends the timer, or keeps it locally when it is shorter than
  ``time_min_entry_seconds`` (D11). Nothing here touches git.
- **Manual overrides automatic (D4).** :func:`decide_activity` is the rule; a
  manual stop opens a pause window in which activity is only noted, when
  ``time_pause_hours`` gives it a length. A person can also turn automatic
  tracking off until they turn it back on (:meth:`Tracker.set_automatic`).
- **Time never runs backwards.** Sessions deliver activity independently, so
  a record older than the running timer's last activity, or than the
  person's last stop, is only noted unless it refreshes the running project.
- **Entries name their runs (issue #648).** A timer gathers the
  :class:`~lmer_timesheet.SessionRef` of each run whose activity started or
  refreshed it, or that started it by hand, and its entry carries them as
  ``sessions``. A switch hands the new run's references to the new timer only.
- **Required handler fields (D19).** A manual start without them is refused;
  an automatic start fills them, or records a prompt and does not start. A
  project whose configured handler is not defined is treated the same way,
  never tracked under ``timesheet``.
- **An automatic entry belongs to a run (issue #665).** Its *identity* is the
  run its activity came from (:mod:`lmer_platform.timetrack_runs`), which
  gives it its title (followed while nobody set one) and its issues. Field
  values follow the entry's *work key*, its first issue or else its run:
  the person's last value for the key, the handler's issue map, then the
  project's last value unless the field is ``per_issue``. Prompts are per
  work key.

Every operation is one ``BEGIN IMMEDIATE`` transaction on the store, because
the API thread and the activity worker call in concurrently. Calls that take
the projects operation lock (a project's key, a new client label) run before
that transaction opens; inside it the tracker only reads the projects
database (a project's idle grace), and nothing holding a projects transaction
calls in here. See docs/TIME-TRACKING.md.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Mapping, Optional

from lmer_timesheet import (
    DEFAULT_HANDLER, Entry, HandlerDefinition, SessionRef, Trigger,
    format_timestamp, is_person_key, issue_label, new_entry_id, validate_fields,
)

from . import projects, timesheet_handlers, timesheet_store, timetrack_runs
from .config import PlatformConfig, time_settings

__all__ = [
    "MANUAL", "AUTO", "STARTED", "SWITCHED", "REFRESHED", "UPDATED", "NOTED",
    "STOPPED", "KEPT", "REFUSED", "PROMPTED", "ADOPTED", "TAKEN_OVER", "REMEMBERED", "SPLIT", "OUTCOMES",
    "START_TRIGGERS",
    "STOP_TRIGGERS", "MAX_TIMER_SESSIONS", "Timer", "Result", "Tracker", "decide_activity",
    "decide_manual_start", "decide_run", "select_run", "is_idle", "is_paused", "register_tracker",
    "get_tracker", "start_tracking_time", "stop_tracking_time",
]

MANUAL = "manual"
AUTO = "auto"

STARTED = "started"
SWITCHED = "switched"
REFRESHED = "refreshed"
UPDATED = "updated"
NOTED = "noted"
STOPPED = "stopped"
KEPT = "kept"
REFUSED = "refused"
PROMPTED = "prompted"
#: A run-less automatic timer took the run of activity in its project (D22).
ADOPTED = "adopted"
#: A timer younger than the minimum entry length took a new run in place (D22).
TAKEN_OVER = "taken_over"
#: A prompt's answer was only remembered for its key: a manual timer runs (D25).
REMEMBERED = "remembered"
#: An edit after time accrued ended the entry and went on in a new one (D24).
SPLIT = "split"
OUTCOMES = (STARTED, SWITCHED, REFRESHED, UPDATED, NOTED, STOPPED, KEPT, REFUSED, PROMPTED,
            ADOPTED, TAKEN_OVER, REMEMBERED, SPLIT)

START_TRIGGERS = ("manual", "activity")
STOP_TRIGGERS = ("manual", "idle", "switch")
MAX_VALUE_CHARS = 200
#: How many run references one timer gathers; past it the earliest are kept,
#: so a day-long timer refreshed from many runs stays a readable entry.
MAX_TIMER_SESSIONS = 50

_TITLE = "title"
_CLIENT = "client"
_FIELD = "field:"
#: Where a running timer's title came from: its run, a person, or the project's
#: most recent title.
TITLE_RUN = "run"
TITLE_PERSON = "person"
TITLE_RECENT = "recent"
#: A field value the person's own memory for the work key supplied (D25.1).
FROM_KEY = "key"
#: The person's latest stop, a ``recents`` row under no project that the
#: prune keeps, one per person: a late activity record never starts time
#: before it, however long its delivery took.
_STOP = "stop"


@dataclass(frozen=True)
class Timer:
    """A person's running timer. Times are UTC, whole seconds."""

    person: str
    workspace_id: str
    project_key: str
    start: datetime
    last_activity: datetime
    mode: str
    start_trigger: str
    handler: str = DEFAULT_HANDLER
    client: str = ""
    title: str = ""
    fields: dict = field(default_factory=dict)
    sessions: tuple = ()
    #: The run this entry is for, or ``None`` (D22).
    identity: Optional[SessionRef] = None
    issues: tuple = ()
    #: The first issue, else the identity run's key, else ``""`` (D25).
    work_key: str = ""
    title_source: str = ""
    #: ``{"client" | "field:<key>": "person" | "key"}``: values a person set,
    #: and field values their memory for the work key supplied.
    sources: dict = field(default_factory=dict)
    #: ``per_issue`` fields left empty for want of an issue (D25).
    unassigned: tuple = ()
    #: A manual timer's latest activity in its project, which orders its runs'
    #: activity (D27's late rule); its times stay the person's own.
    seen: Optional[datetime] = field(default=None, compare=False)

    def to_dict(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "project_key": self.project_key,
            "mode": self.mode,
            "start": format_timestamp(self.start),
            "last_activity": format_timestamp(self.last_activity),
            "start_trigger": self.start_trigger,
            "handler": self.handler,
            "client": self.client,
            "title": self.title,
            "fields": dict(self.fields),
            "sessions": [ref.to_dict() for ref in self.sessions],
            "identity": self.identity.to_dict() if self.identity else None,
            "issues": list(self.issues),
            "work_key": self.work_key,
            "title_source": self.title_source,
            "sources": dict(self.sources),
            "unassigned": list(self.unassigned),
        }


@dataclass(frozen=True)
class Result:
    """What one tracker call did. ``outcome`` is one of :data:`OUTCOMES`.

    ``timer`` is the person's running timer after the call. ``entry`` is the
    entry the call finished, if any: queued for the writer as ``seq``, or kept
    locally when ``kept`` gives the reason. ``missing`` names the handler
    fields that refused a manual start or held back an automatic one.
    """

    outcome: str
    reasons: tuple = ()
    warnings: tuple = ()
    missing: tuple = ()
    timer: Optional[Timer] = None
    entry: Optional[Entry] = None
    seq: Optional[int] = None
    kept: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.outcome != REFUSED

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "ok": self.ok,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "missing": list(self.missing),
            "timer": self.timer.to_dict() if self.timer else None,
            "entry": self.entry.to_dict() if self.entry else None,
            "seq": self.seq,
            "kept": self.kept,
        }


@dataclass(frozen=True)
class _Finished:
    entry: Entry
    seq: Optional[int]
    kept: Optional[str]


@dataclass(frozen=True)
class _Project:
    workspace_id: str
    key: str
    name: str
    archived: bool
    handler_name: str
    #: ``None`` while the configured handler is not defined; the warning says why.
    handler: Optional[HandlerDefinition]
    handler_warning: Optional[str]
    clients: tuple


# --- D4, as pure functions over the stored state ------------------------------

def is_paused(pause_until: Optional[datetime], at: datetime) -> bool:
    """A manual stop's pause window still covers *at*."""
    return pause_until is not None and at < pause_until


def is_idle(timer: Timer, at: datetime, grace_minutes: int) -> bool:
    """*timer* is automatic and its last activity is a full grace before *at*.

    A manual timer is never idle (D4).
    """
    return timer.mode == AUTO and at - timer.last_activity >= timedelta(minutes=grace_minutes)


def decide_activity(timer: Optional[Timer], workspace_id: str, *, paused: bool,
                    late: bool = False, automatic: bool = True) -> str:
    """What one human message in *workspace_id* does: start, switch, refresh or note.

    *timer* is the person's running timer once an idle one has been closed.
    *automatic* is false while the person has automatic tracking turned off:
    then activity only notes, and a running automatic timer is not refreshed.
    *late* says the message is older than the running timer's last activity
    or, with none running, than the person's last stop: sessions deliver
    independently, so it was decided out of order. It may refresh the running
    project, but it never starts or switches, so time never runs backwards.
    """
    if timer is not None and timer.mode == MANUAL:
        return NOTED
    if paused or not automatic:
        return NOTED
    if timer is not None and timer.workspace_id == workspace_id:
        return REFRESHED
    if late:
        return NOTED
    if timer is None:
        return STARTED
    return SWITCHED


def decide_manual_start(timer: Optional[Timer], workspace_id: str) -> str:
    """What a manual start on *workspace_id* does to *timer*.

    A manual timer already on the project keeps running with the new values;
    anything else running ends, so the new values only cover new time.
    """
    if timer is None:
        return STARTED
    if timer.mode == MANUAL and timer.workspace_id == workspace_id:
        return REFRESHED
    return SWITCHED


def decide_run(timer: Timer, run, *, late: bool, young: bool, values_fit: bool) -> str:
    """What activity from *run* (``RunFacts`` or ``None``) does to *timer*, a
    timer the activity's project already runs (D22, D23).

    *young*: the timer is younger than ``time_min_entry_seconds``.
    *values_fit*: taking or adopting *run* changes no value a person or the
    entry already holds and leaves no required field without one.
    """
    if late or run is None:
        return REFRESHED
    if timer.identity is None:
        return ADOPTED if values_fit else SWITCHED
    if timetrack_runs.same_run(timer.identity, run.ref):
        return REFRESHED
    if young and values_fit:
        return TAKEN_OVER
    return SWITCHED


def select_run(timer: Optional[Timer], runs) -> Optional["timetrack_runs.RunFacts"]:
    """The run one activity is for (D21): its one run, or among several the
    running timer's own run; otherwise ``None``, a run-less activity."""
    runs = tuple(runs or ())
    if len(runs) == 1:
        return runs[0]
    if timer is not None and timer.identity is not None:
        for facts in runs:
            if timetrack_runs.same_run(facts.ref, timer.identity):
                return facts
    return None


# --- storage ----------------------------------------------------------------

def _at(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{value!r} is not a timezone-aware datetime")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _stamp(value: datetime) -> str:
    """Fixed-width UTC text, so stored times compare and sort as strings."""
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _merge_sessions(current, added) -> tuple:
    """*current* then each new reference in *added*, up to :data:`MAX_TIMER_SESSIONS`."""
    merged = list(current)
    for ref in added or ():
        if len(merged) >= MAX_TIMER_SESSIONS:
            break
        if ref not in merged:
            merged.append(ref)
    return tuple(merged)


def _refs(items) -> tuple:
    """Stored ``{"run", "uid"}`` items back as references; one that no longer
    validates is dropped rather than failing the timer it rides on."""
    refs = []
    for item in items or ():
        try:
            refs.append(SessionRef(run=item.get("run") or "", uid=item.get("uid") or ""))
        except (AttributeError, ValueError):
            continue
    return tuple(refs)


def _latest(timer: Timer) -> datetime:
    """The latest activity *timer* has seen: activity before it is late."""
    if timer.mode == MANUAL and timer.seen is not None:
        return max(timer.seen, timer.last_activity)
    return timer.last_activity


def _ref(item) -> Optional[SessionRef]:
    refs = _refs([item]) if isinstance(item, dict) else ()
    return refs[0] if refs else None


def _read_timer(db, person: str) -> Optional[Timer]:
    """The running timer; a row written before #665 reads with no identity."""
    row = db.execute("SELECT * FROM timers WHERE person=?", (person,)).fetchone()
    if row is None:
        return None
    meta = json.loads(row["metadata"])
    return Timer(
        person=row["person"],
        workspace_id=row["workspace_id"],
        project_key=row["project_key"],
        start=_parse(row["start"]),
        last_activity=_parse(row["last_activity"]),
        mode=row["mode"],
        start_trigger=row["start_trigger"],
        handler=meta.get("handler") or DEFAULT_HANDLER,
        client=meta.get("client") or "",
        title=meta.get("title") or "",
        fields=dict(meta.get("fields") or {}),
        sessions=_refs(meta.get("sessions")),
        identity=_ref(meta.get("identity")),
        issues=tuple(item for item in meta.get("issues") or () if isinstance(item, str)),
        work_key=meta.get("work_key") or "",
        title_source=meta.get("title_source") or "",
        sources=dict(meta.get("sources") or {}),
        unassigned=tuple(meta.get("unassigned") or ()),
        seen=_parse(meta["seen"]) if meta.get("seen") else None,
    )


def _write_timer(db, timer: Timer) -> None:
    metadata = {
        "handler": timer.handler,
        "client": timer.client,
        "title": timer.title,
        "fields": timer.fields,
        "sessions": [ref.to_dict() for ref in timer.sessions],
        "identity": timer.identity.to_dict() if timer.identity else None,
        "issues": list(timer.issues),
        "work_key": timer.work_key,
        "title_source": timer.title_source,
        "sources": timer.sources,
        "unassigned": list(timer.unassigned),
        "seen": _stamp(timer.seen) if timer.seen else None,
    }
    db.execute(
        "INSERT OR REPLACE INTO timers"
        " (person, workspace_id, project_key, start, last_activity, mode, start_trigger, metadata)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (timer.person, timer.workspace_id, timer.project_key, _stamp(timer.start),
         _stamp(timer.last_activity), timer.mode, timer.start_trigger, json.dumps(metadata)),
    )


def _read_pause(db, person: str) -> Optional[datetime]:
    row = db.execute("SELECT until FROM pauses WHERE person=?", (person,)).fetchone()
    return _parse(row["until"]) if row else None


def _read_auto_off(db, person: str) -> Optional[datetime]:
    """When *person* turned automatic tracking off, or ``None`` while it is on."""
    row = db.execute("SELECT since FROM auto_off WHERE person=?", (person,)).fetchone()
    return _parse(row["since"]) if row else None


def _recent(db, person: str, project_key: str, kind: str, cutoff: str) -> list:
    return [row["value"] for row in db.execute(
        "SELECT value FROM recents WHERE person=? AND project_key=? AND kind=? AND last_used>=?"
        " ORDER BY last_used DESC, value",
        (person, project_key, kind, cutoff),
    )]


def _remember(db, person: str, project_key: str, *, at: datetime, cutoff: str,
              client: Optional[str] = None, title: Optional[str] = None,
              fields: Optional[Mapping] = None, handler: str = "",
              work_key: str = "", key_fields: Optional[Mapping] = None) -> None:
    """Record the values used at *at* and drop the prompts they now satisfy.

    *fields* are remembered for the project and, with a *work_key*, for
    that key under *handler*; *key_fields*, when given, replace them for the
    key (D25).
    """
    if work_key and handler:
        for key, value in ((fields if key_fields is None else key_fields) or {}).items():
            if value:
                db.execute(
                    "INSERT INTO key_values (person, handler, work_key, field, value, last_used)"
                    " VALUES (?,?,?,?,?,?) ON CONFLICT (person, handler, work_key, field)"
                    " DO UPDATE SET value=excluded.value, last_used=excluded.last_used"
                    " WHERE excluded.last_used >= key_values.last_used",
                    (person, handler, work_key, key, value, _stamp(at)),
                )
    used = [(_CLIENT, client), (_TITLE, title)]
    used += [(_FIELD + key, value) for key, value in (fields or {}).items()]
    for kind, value in used:
        if not value:
            continue
        db.execute(
            "INSERT INTO recents (person, project_key, kind, value, last_used) VALUES (?,?,?,?,?)"
            " ON CONFLICT (person, project_key, kind, value)"
            " DO UPDATE SET last_used=max(last_used, excluded.last_used)",
            (person, project_key, kind, value, _stamp(at)),
        )
    for row in db.execute(
        "SELECT id, work_key, run, issues, missing FROM key_prompts WHERE person=? AND project_key=?",
        (person, project_key),
    ).fetchall():
        missing = json.loads(row["missing"])
        # A missing handler (no "key") is never satisfied by a remembered value.
        if all("key" in item and (
                (row["work_key"] and handler
                 and _key_value(db, person, handler, _prompt_row_keys(row), item["key"]))
                or (not item.get("per_issue")
                    and _recent(db, person, project_key, _FIELD + item["key"], cutoff)))
               for item in missing):
            db.execute("DELETE FROM key_prompts WHERE id=?", (row["id"],))


def _key_value(db, person: str, handler: str, keys, field_key: str) -> Optional[str]:
    """The person's latest value for *field_key* under any of *keys* (D25.1)."""
    keys = tuple(keys)
    if not keys:
        return None
    row = db.execute(
        "SELECT value FROM key_values WHERE person=? AND handler=? AND field=?"
        f" AND work_key IN ({','.join('?' * len(keys))}) ORDER BY last_used DESC LIMIT 1",
        (person, handler, field_key, *keys),
    ).fetchone()
    return row["value"] if row else None


def _work_keys(timer_or_facts) -> tuple:
    """The keys a work key is matched by: an issue as it is, a run key with
    its aliases (D25)."""
    key = timer_or_facts.work_key
    if not key:
        return ()
    if timer_or_facts.issues:
        return (key,)
    ref = timer_or_facts.identity if isinstance(timer_or_facts, Timer) else timer_or_facts.ref
    keys = list(timetrack_runs.run_keys(ref)) if ref is not None else []
    return tuple(dict.fromkeys([key, *keys]))


def _own_keys(timer: "Timer") -> tuple:
    """*timer*'s work key with its run's aliases: every key a prompt for it
    may be stored under (D25.1)."""
    return tuple(dict.fromkeys([timer.work_key, *_work_keys(timer)]))


def _prompt_run(row) -> Optional[SessionRef]:
    return _ref(json.loads(row["run"])) if row["run"] else None


def _prompt_row_keys(row) -> tuple:
    """A prompt row's work key with its run's aliases (D25.1)."""
    key = row["work_key"]
    if not key or json.loads(row["issues"] or "[]"):
        return (key,)
    ref = _prompt_run(row)
    keys = timetrack_runs.run_keys(ref) if ref is not None else ()
    return tuple(dict.fromkeys([key, *keys]))


def _clear_prompts(db, person: str, workspace_id: str, work_keys, run=None) -> None:
    """Drop the prompts under *work_keys* and, with *run*, every prompt of
    that run: a run keeps its prompt as it gains issues (D25.1)."""
    for key in dict.fromkeys(work_keys):
        db.execute("DELETE FROM key_prompts WHERE person=? AND workspace_id=? AND work_key=?",
                   (person, workspace_id, key))
    if run is None:
        return
    for row in db.execute("SELECT id, run FROM key_prompts WHERE person=? AND workspace_id=?"
                          " AND run != ''", (person, workspace_id)).fetchall():
        if timetrack_runs.same_run(_prompt_run(row), run):
            db.execute("DELETE FROM key_prompts WHERE id=?", (row["id"],))


def _mark_stop(db, person: str, at: datetime) -> None:
    db.execute(
        "INSERT INTO recents (person, project_key, kind, value, last_used) VALUES (?,'',?,'',?)"
        " ON CONFLICT (person, project_key, kind, value)"
        " DO UPDATE SET last_used=max(last_used, excluded.last_used)",
        (person, _STOP, _stamp(at)),
    )


def _last_stop(db, person: str) -> Optional[datetime]:
    row = db.execute(
        "SELECT last_used FROM recents WHERE person=? AND project_key='' AND kind=?", (person, _STOP),
    ).fetchone()
    return _parse(row["last_used"]) if row else None


def _failing_fields(handler: HandlerDefinition, values: Mapping) -> list:
    problems = validate_fields(handler, values)
    return [item for item in handler.fields
            if any(problem.startswith(f"{item.key}:") for problem in problems)]


def _labels(items) -> str:
    return " and ".join(item["label"] for item in items)


def _settings() -> dict:
    return {key: setting.value for key, setting in time_settings().items()}


def _cutoff(now: datetime, settings: dict) -> str:
    return _stamp(now - timedelta(days=settings["title_max_age_days"]))


def _text(value) -> Optional[str]:
    return None if value is None else str(value).strip()


def _too_long(title: Optional[str], fields: Mapping) -> list:
    problems = []
    if title and len(title) > MAX_VALUE_CHARS:
        problems.append(f"title: longer than {MAX_VALUE_CHARS} characters")
    for key, value in fields.items():
        if len(value) > MAX_VALUE_CHARS:
            problems.append(f"{key}: longer than {MAX_VALUE_CHARS} characters")
    return problems


# --- the tracker ----------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Tracker:
    """Timers, the pause window, automatic on/off, recent values and prompts, for every person.

    Holds no state of its own beyond *config*, the clock and whose tracking
    :meth:`stop_running` ended, so any number of threads may call it; *now* is
    injectable so no test waits on the wall clock.
    """

    def __init__(self, config: PlatformConfig, *, now: Callable[[], datetime] = _utc_now,
                 run_facts: Callable = timetrack_runs.facts_for):
        self.config = config
        self._now = now
        #: ``SessionRef -> RunFacts | None``: what a run says now (its title).
        self._run_facts = run_facts
        self._ended_all = False
        self._ended: set = set()

    def now(self) -> datetime:
        """The tracker's clock, in whole UTC seconds: what a manual start or stop is stamped with."""
        return _at(self._now())

    # --- the two calls ---------------------------------------------------

    def start(self, person: str, workspace_id: str, *, at: datetime, trigger: str,
              client: Optional[str] = None, title: Optional[str] = None,
              fields: Optional[Mapping] = None, runs=(), sessions=()) -> Result:
        """Start *person*'s time on *workspace_id* (the platform project id).

        ``trigger="manual"`` is a start by hand; ``"activity"`` is an automatic
        start, subject to D4 (it may only be noted) and to D19's prompt.
        ``None`` for *client* or *title* means "not given": the project's first
        client label and the run's title, else the most recent title, fill in.
        A client label the project does not list yet is added to it (D12).
        *runs* are the :class:`~lmer_platform.timetrack_runs.RunFacts` of the
        runs this call came from (:func:`select_run` picks the entry's run);
        *sessions* are their :class:`~lmer_timesheet.SessionRef`, which a timer
        that starts or keeps running gathers.
        """
        if trigger not in START_TRIGGERS:
            raise ValueError(f"start trigger {trigger!r} is not one of {START_TRIGGERS}")
        at = _at(at)
        refusal = self._refusal(person)
        if refusal is not None:
            return refusal
        try:
            project = self._project(workspace_id)
        except projects.ProjectError as exc:
            return Result(REFUSED, reasons=(str(exc),))
        manual = trigger == "manual"
        if project.archived:
            reason = f"{project.name} is archived; restore it first"
            return Result(REFUSED if manual else NOTED, reasons=(reason,))
        if project.handler is None:
            if manual:
                return Result(REFUSED, reasons=(project.handler_warning,))
            given, warnings = {}, []
        else:
            given, warnings = _given_fields(project.handler, fields, keep_empty=False)
        title = _text(title)
        problems = _too_long(title, given)
        if problems:
            return Result(REFUSED, reasons=tuple(problems))
        if manual:
            failing = _failing_fields(project.handler, given)
            if failing:
                return Result(REFUSED, reasons=tuple(validate_fields(project.handler, given)),
                              missing=tuple(item.key for item in failing))
        try:
            client = self._client(project, _text(client))
        except projects.ProjectError as exc:
            return Result(REFUSED, reasons=(str(exc),))
        if project.handler_warning:
            warnings.append(project.handler_warning)
        settings = _settings()
        issue_map, _problems = (timesheet_handlers.issue_map(self.config, project.handler)
                                if project.handler is not None else ({}, []))
        begin = self._start_manual if manual else self._start_auto
        with timesheet_store.database() as db:
            ended = self._ended_refusal(person)
            if ended is not None:
                return ended
            result = begin(db, person, project, at=at, client=client, title=title,
                           fields=given, runs=tuple(runs or ()), issue_map=issue_map,
                           sessions=_merge_sessions((), sessions), settings=settings)
        result = replace(result, warnings=tuple(warnings) + result.warnings)
        if result.seq is not None:
            timesheet_store.notify_writer()
        return result

    def stop(self, person: str, workspace_id: Optional[str], *, at: datetime, trigger: str) -> Result:
        """Stop *person*'s running timer and finish its entry.

        *workspace_id*, when given, must be the running timer's project. With
        ``time_pause_hours`` above 0 (it defaults to 0), a manual stop opens the
        pause window (D4) even when nothing runs.
        """
        if trigger not in STOP_TRIGGERS:
            raise ValueError(f"stop trigger {trigger!r} is not one of {STOP_TRIGGERS}")
        at = _at(at)
        refusal = self._refusal(person)
        if refusal is not None:
            return refusal
        settings = _settings()
        with timesheet_store.database() as db:
            timer = _read_timer(db, person)
            if timer is not None and workspace_id is not None and timer.workspace_id != workspace_id:
                return Result(REFUSED, timer=timer, reasons=(
                    f"the running timer is on {timer.project_key}, not on this project",))
            if trigger == "manual":
                db.execute("DELETE FROM pauses WHERE person=?", (person,))
                if settings["pause_hours"]:
                    until = at + timedelta(hours=settings["pause_hours"])
                    db.execute("INSERT INTO pauses (person, until) VALUES (?,?)", (person, _stamp(until)))
            if timer is None:
                return Result(NOTED, reasons=("no timer is running",))
            _, finished = self._close_if_idle(db, timer, at, settings)
            if finished is None:
                finished = self._finish(db, timer, end=self._end_at(db, timer, at), stop=trigger,
                                        settings=settings)
        if finished.seq is not None:
            timesheet_store.notify_writer()
        return Result(KEPT if finished.kept else STOPPED, entry=finished.entry, seq=finished.seq,
                      kept=finished.kept, reasons=(finished.kept,) if finished.kept else ())

    # --- what the activity consumer and the API add ----------------------

    def note_activity(self, person: str, workspace_id: str, *, at: datetime,
                      runs=(), sessions=()) -> Result:
        """One human message from *person* in *workspace_id* at *at* (D4, D5),
        from the runs *runs* describes and *sessions* names (D21)."""
        return self.start(person, workspace_id, at=at, trigger="activity", runs=runs,
                          sessions=sessions)

    def answer_prompt(self, person: str, prompt_id: int, *, at: Optional[datetime] = None,
                      client: Optional[str] = None, title: Optional[str] = None,
                      fields: Optional[Mapping] = None, sessions=()) -> Result:
        """Answer *person*'s prompt *prompt_id* with *fields* (D25).

        An idle timer ends first; a prompt that this satisfies is no longer
        open and the answer is refused. The values are validated as a manual
        start's are. A running timer the prompt is for (its key, or its run
        for a prompt under a run key) is filled when unassigned; one that
        already has its values refuses the answer and drops the prompt.
        Otherwise the values are remembered for the prompt's work key,
        then: with a manual timer running anywhere they are only remembered
        (:data:`REMEMBERED`); with automatic tracking off or paused a manual
        timer starts; otherwise an automatic timer starts with the prompt's
        run and issues, ``trigger.start: manual``. An accepted answer drops
        its prompt.
        """
        at = _at(at if at is not None else self._now())
        refusal = self._refusal(person)
        if refusal is not None:
            return refusal
        with timesheet_store.database() as db:
            row = db.execute("SELECT * FROM key_prompts WHERE id=?", (int(prompt_id),)).fetchone()
        if row is None or row["person"] != person:
            return Result(REFUSED, reasons=(f"prompt {prompt_id} is not one of {person}'s",))
        try:
            project = self._project(row["workspace_id"])
        except projects.ProjectError as exc:
            return Result(REFUSED, reasons=(str(exc),))
        if project.archived:
            return Result(REFUSED, reasons=(f"{project.name} is archived; restore it first",))
        if project.handler is None:
            return Result(REFUSED, reasons=(project.handler_warning,))
        given, warnings = _given_fields(project.handler, fields, keep_empty=False)
        title = _text(title)
        problems = _too_long(title, given)
        bad = [item for item in _failing_fields(project.handler, given) if item.key in given]
        if problems or bad:
            reasons = tuple(problems) + tuple(problem for problem in validate_fields(project.handler, given)
                                              if problem.split(":", 1)[0] in given)
            return Result(REFUSED, reasons=reasons, missing=tuple(item.key for item in bad))
        try:
            client = self._client(project, _text(client))
        except projects.ProjectError as exc:
            return Result(REFUSED, reasons=(str(exc),))
        stored = json.loads(row["run"]) if row["run"] else {}
        ref = _ref(stored)
        issues = tuple(json.loads(row["issues"] or "[]"))
        facts = None
        if ref is not None:
            fresh = self._facts(ref)
            facts = timetrack_runs.RunFacts(
                ref=ref, key=row["work_key"] if not issues else (ref.run or row["work_key"]),
                title=(fresh.title if fresh is not None else stored.get("title")) or "",
                issues=issues)
        issue_map, _problems = timesheet_handlers.issue_map(self.config, project.handler)
        settings = _settings()
        with timesheet_store.database() as db:
            ended = self._ended_refusal(person)
            if ended is not None:
                return ended
            result = self._answer(db, person, project, row, facts, at=at, client=client,
                                  title=title, given=given, issue_map=issue_map,
                                  sessions=_merge_sessions((), sessions), settings=settings)
        result = replace(result, warnings=tuple(warnings) + result.warnings)
        if result.seq is not None:
            timesheet_store.notify_writer()
        return result

    def prompt_workspace(self, person: str, prompt_id: int) -> Optional[str]:
        """The project *person*'s prompt *prompt_id* is for, or ``None``."""
        with timesheet_store.database() as db:
            row = db.execute("SELECT person, workspace_id FROM key_prompts WHERE id=?",
                             (int(prompt_id),)).fetchone()
        return row["workspace_id"] if row is not None and row["person"] == person else None

    def update_running(self, person: str, *, client: Optional[str] = None,
                       title: Optional[str] = None, fields: Optional[Mapping] = None,
                       base: Optional[Mapping] = None) -> Result:
        """Edit the running timer's values (D8, D24).

        ``None`` leaves a value as it is; an empty string clears it. A field
        edit that the handler refuses (a required field cleared, a pattern
        not matched) refuses the whole call. *base* holds the values the
        caller's form loaded: a value equal to its base was not edited. An
        idle timer is closed first. Changing a non-empty value once
        ``time_min_entry_seconds`` has accrued splits the entry
        (:data:`SPLIT`); filling an empty one edits in place.
        """
        refusal = self._refusal(person)
        if refusal is not None:
            return refusal
        settings = _settings()
        with timesheet_store.database() as db:
            timer, closed = self._close_if_idle(db, _read_timer(db, person), _at(self._now()), settings)
        if closed is not None and closed.seq is not None:
            timesheet_store.notify_writer()
        if timer is None:
            return _with_finished(Result(REFUSED, reasons=("no timer is running",)), closed)
        client, title, fields = _unedited_dropped(base, client=client, title=title, fields=fields)
        try:
            project = self._project(timer.workspace_id)
        except projects.ProjectError as exc:
            return Result(REFUSED, reasons=(str(exc),), timer=timer)
        handler, handler_warning = timesheet_handlers.lookup(self.config, timer.handler)
        if handler is None:
            # Title and client still edit; fields cannot be checked against a
            # definition that is not there, and the entry keeps the timer's handler.
            if fields:
                return Result(REFUSED, reasons=(handler_warning,), timer=timer)
            given, warnings = {}, [handler_warning]
        else:
            given, warnings = _given_fields(handler, fields, keep_empty=True)
        title = _text(title)
        problems = _too_long(title, given)
        if problems:
            return Result(REFUSED, reasons=tuple(problems), timer=timer)
        try:
            client = self._client(project, _text(client))
        except projects.ProjectError as exc:
            return Result(REFUSED, reasons=(str(exc),), timer=timer)
        with timesheet_store.database() as db:
            current = _read_timer(db, person)
            if current is None or (current.workspace_id, current.start) != (timer.workspace_id, timer.start):
                return Result(REFUSED, timer=current,
                              reasons=("the running timer changed meanwhile; check it and try again",))
            merged = {key: value for key, value in {**current.fields, **given}.items() if value}
            failing = [item for item in _failing_fields(handler, merged) if item.key in given] if handler else []
            if failing:
                reasons = [problem for problem in validate_fields(handler, merged)
                           if problem.split(":", 1)[0] in given]
                return Result(REFUSED, reasons=tuple(reasons), timer=current,
                              missing=tuple(item.key for item in failing))
            now = _at(self._now())
            updated, finished = self._edit(db, current, client=client, title=title, fields=given,
                                           at=now, settings=settings)
            _write_timer(db, updated)
            _remember(db, person, updated.project_key, at=now, cutoff=_cutoff(now, settings),
                      client=client, title=title,
                      fields={key: value for key, value in given.items() if value},
                      handler=updated.handler, work_key=updated.work_key)
        if finished is not None and finished.seq is not None:
            timesheet_store.notify_writer()
        return _with_finished(Result(SPLIT if finished else UPDATED, timer=updated,
                                     warnings=tuple(warnings)), finished)

    def set_automatic(self, person: str, on: bool) -> Result:
        """Turn *person*'s automatic tracking on or off (D4); off stays until turned on.

        While it is off, activity is only noted for automatic tracking: nothing
        starts, switches or prompts for it, and a running automatic timer gets
        no more time: whatever ends it, turning tracking back on included, ends
        it at its last activity. A running manual timer still follows runs in
        its project (D27), and manual start, stop and edits work as ever. The result carries the
        running timer.
        """
        refusal = self._refusal(person)
        if refusal is not None:
            return refusal
        finished = None
        settings = _settings()
        with timesheet_store.database() as db:
            timer = _read_timer(db, person)
            if on:
                if timer is not None and timer.mode == AUTO and _read_auto_off(db, person) is not None:
                    finished = self._finish(db, timer, end=timer.last_activity, stop="idle",
                                            settings=settings)
                    timer = None
                db.execute("DELETE FROM auto_off WHERE person=?", (person,))
            else:
                db.execute("INSERT OR IGNORE INTO auto_off (person, since) VALUES (?,?)",
                           (person, _stamp(self.now())))
        if finished is not None and finished.seq is not None:
            timesheet_store.notify_writer()
        return _with_finished(Result(UPDATED, timer=timer), finished)

    def close_idle(self, now: Optional[datetime] = None) -> list:
        """Close every automatic timer idle at *now*, stamped at its last activity (D1).

        The daemon also runs this at start-up, which closes a timer left open
        across a restart (D7). It prunes, by ``time_title_max_age_days``,
        recent values, kept slivers and prompts, and drops expired pause
        windows. Returns one :class:`Result` per closed timer.
        """
        now = _at(now if now is not None else self._now())
        settings = _settings()
        results = []
        with timesheet_store.database() as db:
            people = [row["person"] for row in db.execute(
                "SELECT person FROM timers WHERE mode=? ORDER BY person", (AUTO,))]
            for person in people:
                timer = _read_timer(db, person)
                if not is_idle(timer, now, self._grace_minutes(timer.workspace_id, settings)):
                    continue
                finished = self._finish(db, timer, end=timer.last_activity, stop="idle", settings=settings)
                results.append(Result(KEPT if finished.kept else STOPPED, entry=finished.entry,
                                      seq=finished.seq, kept=finished.kept,
                                      reasons=(finished.kept,) if finished.kept else ()))
            cutoff = _cutoff(now, settings)
            db.execute("DELETE FROM recents WHERE last_used<? AND kind!=?", (cutoff, _STOP))
            db.execute("DELETE FROM kept WHERE at<?", (cutoff,))
            db.execute("DELETE FROM key_prompts WHERE at<?", (cutoff,))
            db.execute("DELETE FROM key_values WHERE last_used<?", (cutoff,))
            db.execute("DELETE FROM pauses WHERE until<=?", (_stamp(now),))
        if any(result.seq is not None for result in results):
            timesheet_store.notify_writer()
        return results

    def stop_running(self, people: Optional[Iterable[str]] = None, *,
                     at: Optional[datetime] = None) -> list:
        """Close the running timers of *people* (everyone's when ``None``), whose
        tracking is ending: switched off, or the person removed.

        An automatic timer ends at its last activity (``idle``), as the start-up
        sweep ends one; a manual timer ends at *at* (``manual``). From then on
        this tracker refuses to start a timer for them until :meth:`resume`.
        Returns one :class:`Result` per closed timer.
        """
        at = _at(at if at is not None else self._now())
        wanted = None if people is None else set(people)
        settings = _settings()
        results = []
        with timesheet_store.database() as db:
            # Under the store's write lock, so a start already past _refusal
            # either committed before this (and is closed below) or sees the flag.
            if wanted is None:
                self._ended_all = True
            else:
                self._ended |= wanted
            running = [row["person"] for row in db.execute("SELECT person FROM timers ORDER BY person")]
            for person in running:
                if wanted is not None and person not in wanted:
                    continue
                timer = _read_timer(db, person)
                if timer.mode == AUTO:
                    end, stop = min(timer.last_activity, at), "idle"
                else:
                    end, stop = at, "manual"
                finished = self._finish(db, timer, end=end, stop=stop, settings=settings)
                results.append(Result(KEPT if finished.kept else STOPPED, entry=finished.entry,
                                      seq=finished.seq, kept=finished.kept,
                                      reasons=(finished.kept,) if finished.kept else ()))
        if any(result.seq is not None for result in results):
            timesheet_store.notify_writer()
        return results

    def resume(self, people: Iterable[str]) -> None:
        """Let *people*, listed again, start timers that :meth:`stop_running` ended."""
        self._ended -= set(people)

    def status(self, person: str) -> dict:
        """*person*'s running timer, warnings, prompts, pause window, whether
        automatic tracking is on, and the outbox.

        ``warnings`` items are ``{"kind", "message"}`` with ``kind`` one of
        ``no_client``, ``missing_field`` (plus ``field`` and ``label``) or
        ``handler``. ``outbox`` is :func:`timesheet_store.outbox_status`;
        ``writer_refusal`` is its ``last_error``.
        """
        now = _at(self._now())
        settings = _settings()
        cutoff = _cutoff(now, settings)
        with timesheet_store.database() as db:
            timer = _read_timer(db, person)
            pause = _read_pause(db, person)
            auto_off = _read_auto_off(db, person)
            prompt_rows = db.execute(
                "SELECT * FROM key_prompts WHERE person=? AND at>=? ORDER BY at DESC, id DESC",
                (person, cutoff),
            ).fetchall()
        warnings = []
        running = None
        if timer is not None:
            name, project_handler = timer.project_key, None
            try:
                row = projects.get(timer.workspace_id)
                name, project_handler = row["name"], row["settings"].get("time_handler")
            except projects.ProjectError:
                pass
            running = {**timer.to_dict(), "project_name": name,
                       "elapsed_seconds": max(0, int((now - timer.start).total_seconds())),
                       "idle_at": None}
            if timer.mode == AUTO:
                grace = self._grace_minutes(timer.workspace_id, settings)
                running["idle_at"] = format_timestamp(timer.last_activity + timedelta(minutes=grace))
            if not timer.client:
                warnings.append({"kind": "no_client", "message": f"{name} has no client label"})
            handler, _warning = timesheet_handlers.lookup(self.config, timer.handler)
            failing = _failing_fields(handler, timer.fields) if handler else []
            failing += [item for item in (handler.fields if handler else ())
                        if item.key in timer.unassigned and item not in failing]
            for item in failing:
                warnings.append({"kind": "missing_field", "field": item.key, "label": item.label,
                                 "message": f"{name} needs a {item.label} to track time"})
            _handler, warning = timesheet_handlers.lookup(self.config, project_handler)
            if warning:
                warnings.append({"kind": "handler", "message": warning})
        prompts = []
        for row in prompt_rows:
            missing = json.loads(row["missing"])
            try:
                name = projects.get(row["workspace_id"])["name"]
            except projects.ProjectError:
                name = row["project_key"]
            issues = json.loads(row["issues"] or "[]")
            stored = json.loads(row["run"]) if row["run"] else {}
            run = _ref(stored)
            if issues:
                label = issue_label(issues[0])
            elif run is not None:
                facts = self._facts(run)
                label = facts.title if facts is not None else stored.get("title") or row["work_key"]
            else:
                label = ""
            prompts.append({
                "id": row["id"], "workspace_id": row["workspace_id"],
                "project_key": row["project_key"], "project_name": name,
                "work_key": row["work_key"], "label": label,
                "run": run.to_dict() if run is not None else None, "issues": issues,
                "missing": missing, "at": format_timestamp(_parse(row["at"])),
                "message": _prompt_message(name, label, missing),
            })
        outbox = timesheet_store.outbox_status()
        return {
            "person": person,
            "enabled": self.config.time_tracking_enabled,
            "problems": list(self.config.time_problems),
            "running": running,
            "warnings": warnings,
            "prompts": prompts,
            "pause": {
                "until": format_timestamp(pause),
                "remaining_seconds": int((pause - now).total_seconds()),
            } if is_paused(pause, now) else None,
            "automatic": {"on": auto_off is None,
                          "off_since": format_timestamp(auto_off) if auto_off else None},
            "outbox": outbox,
            "writer_refusal": outbox["last_error"],
        }

    def recents(self, person: str, workspace_id: str) -> dict:
        """``{"titles", "clients", "fields": {key: [...]}}``, most recently used first (D12)."""
        key = projects.project_key(workspace_id)
        cutoff = _cutoff(_at(self._now()), _settings())
        out = {"titles": [], "clients": [], "fields": {}}
        with timesheet_store.database() as db:
            rows = db.execute(
                "SELECT kind, value FROM recents WHERE person=? AND project_key=? AND last_used>=?"
                " ORDER BY last_used DESC, value", (person, key, cutoff),
            ).fetchall()
        for row in rows:
            if row["kind"] == _TITLE:
                out["titles"].append(row["value"])
            elif row["kind"] == _CLIENT:
                out["clients"].append(row["value"])
            elif row["kind"].startswith(_FIELD):
                out["fields"].setdefault(row["kind"][len(_FIELD):], []).append(row["value"])
        return out

    def kept_entries(self, person: str) -> list:
        """*person*'s entries kept locally for being too short (D11), newest first."""
        cutoff = _cutoff(_at(self._now()), _settings())
        with timesheet_store.database() as db:
            rows = db.execute("SELECT * FROM kept WHERE person=? AND at>=? ORDER BY start DESC",
                              (person, cutoff)).fetchall()
        return [{
            "id": row["id"], "person": row["person"], "workspace_id": row["workspace_id"],
            "project": row["project_key"], "handler": row["handler"],
            "fields": json.loads(row["fields"]), "client": row["client"], "title": row["title"],
            "start": format_timestamp(_parse(row["start"])), "end": format_timestamp(_parse(row["end"])),
            "duration": row["duration"],
            "trigger": {"start": row["trigger_start"], "stop": row["trigger_stop"]},
            "reason": row["reason"], "at": format_timestamp(_parse(row["at"])),
            "sessions": [ref.to_dict() for ref in _refs(json.loads(row["sessions"]))],
            "identity": json.loads(row["identity"]) if row["identity"] else None,
            "issues": json.loads(row["issues"] or "[]"),
        } for row in rows]

    # --- inside one store transaction ------------------------------------

    def _start_manual(self, db, person: str, project: _Project, *, at, client, title,
                      fields, runs, issue_map, sessions, settings) -> Result:
        cutoff = _cutoff(_at(self._now()), settings)
        db.execute("DELETE FROM pauses WHERE person=?", (person,))
        timer, finished = self._close_if_idle(db, _read_timer(db, person), at, settings)
        decision = decide_manual_start(timer, project.workspace_id)
        if decision == REFRESHED:
            updated, split = self._edit(db, timer, client=client, title=title, fields=fields,
                                        at=at, settings=settings)
            updated = replace(updated, sessions=_merge_sessions(updated.sessions, sessions))
            _write_timer(db, updated)
            _remember(db, person, project.key, at=at, cutoff=cutoff, client=client, title=title,
                      fields=fields, handler=updated.handler, work_key=updated.work_key)
            _clear_prompts(db, person, project.workspace_id, ("", *_own_keys(updated)),
                           run=updated.identity)
            return _with_finished(Result(SPLIT if split else REFRESHED, timer=updated), split)
        if decision == SWITCHED:
            finished = self._finish(db, timer, end=self._end_at(db, timer, at), stop="switch",
                                    settings=settings)
        new = Timer(
            person=person, workspace_id=project.workspace_id, project_key=project.key,
            start=at, last_activity=at, mode=MANUAL, start_trigger="manual",
            handler=project.handler.name,
            client=self._default_client(project) if client is None else client,
            fields=dict(fields), sessions=sessions,
            sources=_person_set({}, client, fields),
            **self._identity(db, person, project, select_run(None, runs), title, cutoff),
        )
        _write_timer(db, new)
        _remember(db, person, project.key, at=at, cutoff=cutoff, client=client, title=title,
                  fields=fields, handler=new.handler, work_key=new.work_key)
        _clear_prompts(db, person, project.workspace_id, ("", *_own_keys(new)),
                       run=new.identity)
        return _with_finished(Result(decision, timer=new), finished)

    def _start_auto(self, db, person: str, project: _Project, *, at, client, title,
                    fields, runs, issue_map, sessions, settings) -> Result:
        cutoff = _cutoff(_at(self._now()), settings)
        timer, finished = self._close_if_idle(db, _read_timer(db, person), at, settings)
        pause = _read_pause(db, person)
        auto_off = _read_auto_off(db, person)
        latest = timer.last_activity if timer is not None else _last_stop(db, person)
        paused = is_paused(pause, at)
        decision = decide_activity(timer, project.workspace_id, paused=paused,
                                   late=latest is not None and at < latest, automatic=auto_off is None)
        facts = select_run(timer, runs)
        if decision == NOTED:
            if timer is not None and timer.mode == MANUAL:
                reason = f"a manual timer is running on {timer.project_key}"
                if timer.workspace_id == project.workspace_id and at >= timer.start:
                    followed = self._manual_follows(
                        db, person, project, timer, facts, at=at, sessions=sessions,
                        issue_map=issue_map, cutoff=cutoff, settings=settings)
                    if followed is not None:
                        return _with_finished(followed, finished)
                    # The manual entry covers this activity, so it names the run,
                    # but the timer's mode and times stay the person's own.
                    merged = _merge_sessions(timer.sessions, sessions)
                    updated = _follow_title(replace(timer, sessions=merged,
                                                    seen=max(_latest(timer), at)), facts)
                    # ``seen`` is not compared, so a plain refresh is still saved.
                    if updated != timer or updated.seen != timer.seen:
                        timer = updated
                        _write_timer(db, timer)
            else:
                if auto_off is not None:
                    reason = f"automatic tracking is off since {format_timestamp(auto_off)}"
                elif paused:
                    reason = f"automatic tracking is paused until {format_timestamp(pause)}"
                else:
                    reason = (f"an activity at {format_timestamp(at)} is older than the last one,"
                              f" at {format_timestamp(latest)}")
                # The run-title follow is not activity (D22).
                followed = _follow_title(timer, facts)
                if followed is not timer:
                    timer = followed
                    _write_timer(db, timer)
            return _with_finished(Result(NOTED, reasons=(reason,), timer=timer), finished)
        if decision == REFRESHED:
            changed = self._same_project_run(db, person, project, timer, facts, at=at,
                                             issue_map=issue_map, cutoff=cutoff, settings=settings)
            if changed is not None:
                decision, updated = changed
            else:
                updated = timer
            if decision != SWITCHED:
                updated = _follow_title(replace(updated, last_activity=max(timer.last_activity, at),
                                                sessions=_merge_sessions(timer.sessions, sessions)),
                                        facts)
                _write_timer(db, updated)
                if decision in (ADOPTED, TAKEN_OVER):
                    _clear_prompts(db, person, project.workspace_id, _own_keys(updated),
                                   run=updated.identity)
                return Result(decision, timer=updated)
        if pause is not None:
            db.execute("DELETE FROM pauses WHERE person=?", (person,))
        identity = self._identity(db, person, project, facts, title, cutoff)
        if project.handler is None:
            failing, values, unassigned, marks = [], dict(fields), (), {}
            missing = [{"handler": project.handler_name,
                        "label": f"usable definition of handler {project.handler_name!r}"}]
        else:
            values, failing, unassigned, marks = self._resolve_fields(
                db, person, project, given=fields, facts=facts, issue_map=issue_map, cutoff=cutoff)
            missing = [{"key": item.key, "label": item.label, "per_issue": item.per_issue}
                       for item in failing]
        if missing:
            # The person moved on from the running project, so its time ends
            # here even though the new one cannot start.
            if timer is not None:
                finished = self._finish(db, timer, end=at, stop="switch", settings=settings)
            message = self._prompt(db, person, project, facts, missing, at)
            return _with_finished(Result(
                PROMPTED, missing=tuple(item.key for item in failing), reasons=(message,),
            ), finished)
        if timer is not None:
            finished = self._finish(db, timer, end=at, stop="switch", settings=settings)
        new = Timer(
            person=person, workspace_id=project.workspace_id, project_key=project.key,
            start=at, last_activity=at, mode=AUTO, start_trigger="activity",
            handler=project.handler.name,
            client=self._default_client(project) if client is None else client,
            fields=values, sessions=sessions, unassigned=unassigned,
            sources=_person_set(marks, client, fields), **identity,
        )
        _write_timer(db, new)
        _clear_prompts(db, person, project.workspace_id, _own_keys(new),
                       run=new.identity)
        _remember(db, person, project.key, at=at, cutoff=cutoff, client=client, title=title,
                  fields=fields, handler=new.handler, work_key=new.work_key)
        return _with_finished(Result(decision, timer=new), finished)

    def _edit(self, db, timer: Timer, *, client: Optional[str], title: Optional[str],
              fields: Mapping, at: datetime, settings: dict) -> tuple:
        """``(timer, finished or None)``: *timer* with a person's edit (D24).

        Every edited value is marked as the person's. A changed non-empty
        value, once the minimum has accrued, ends *timer* (``stop: edit``) at
        its last activity (automatic) or at *at* (manual), and the edit goes
        on in a new part from there (``start: edit``) with the same mode,
        project, run and issues, naming only its run.
        """
        def changes(old, new):
            return new is not None and bool(old) and new != old

        changed = (changes(timer.title, title) or changes(timer.client, client)
                   or any(changes(timer.fields.get(key, ""), value) for key, value in fields.items()))
        updated = replace(
            timer,
            client=timer.client if client is None else client,
            title=timer.title if title is None else title,
            title_source=timer.title_source if title is None else TITLE_PERSON,
            fields={key: value for key, value in {**timer.fields, **fields}.items() if value},
            sources=_person_set(timer.sources, client, fields),
            unassigned=tuple(key for key in timer.unassigned if not fields.get(key)),
        )
        point = timer.last_activity if timer.mode == AUTO else at
        if not changed or (point - timer.start).total_seconds() < settings["min_entry_seconds"]:
            return updated, None
        finished = self._finish(db, timer, end=point, stop="edit", settings=settings)
        return replace(updated, start=point, last_activity=point, start_trigger="edit",
                       sessions=(timer.identity,) if timer.identity else ()), finished

    def _answer(self, db, person: str, project: _Project, row, facts, *, at, client, title,
                given, issue_map, sessions, settings) -> Result:
        cutoff = _cutoff(_at(self._now()), settings)
        work_key = row["work_key"]
        # The idle close can remember values that satisfy this very prompt.
        timer, finished = self._close_if_idle(db, _read_timer(db, person), at, settings)
        if db.execute("SELECT 1 FROM key_prompts WHERE id=?", (row["id"],)).fetchone() is None:
            return _with_finished(Result(REFUSED, reasons=(
                f"prompt {row['id']} is no longer open",)), finished)
        if facts is None and work_key:
            return _with_finished(Result(REFUSED, reasons=(
                f"prompt {row['id']} no longer names its run",)), finished)
        values, failing, unassigned, marks = self._resolve_fields(
            db, person, project, given=given, facts=facts, issue_map=issue_map, cutoff=cutoff)
        if failing:
            return _with_finished(Result(REFUSED, reasons=tuple(validate_fields(project.handler, values)),
                                         missing=tuple(item.key for item in failing)), finished)
        # A prompt under a run key belongs to that run whatever its work key is now;
        # one under an issue key belongs to that issue only (D25, Q13).
        own = (timer is not None and timer.workspace_id == project.workspace_id
               and (work_key in _own_keys(timer)
                    or (not json.loads(row["issues"] or "[]")
                        and timetrack_runs.same_run(_prompt_run(row), timer.identity))))
        if own and not timer.unassigned:
            db.execute("DELETE FROM key_prompts WHERE id=?", (row["id"],))
            return _with_finished(Result(REFUSED, timer=timer, reasons=(
                f"the running timer on {timer.project_key} already has its values; edit it"
                " instead",)), finished)
        paused = is_paused(_read_pause(db, person), at) or _read_auto_off(db, person) is not None
        if not own and paused and unassigned and (timer is None or timer.mode != MANUAL):
            # A new manual timer validates fully (D25, D27): only an
            # automatic run-less start may run unassigned.
            reasons = [problem for problem in validate_fields(project.handler, values)
                       if problem.split(":", 1)[0] in unassigned]
            return _with_finished(Result(REFUSED, missing=unassigned, reasons=tuple(reasons)),
                                  finished)
        _remember(db, person, project.key, at=at, cutoff=cutoff, client=client, title=title,
                  fields=given, handler=project.handler.name, work_key=work_key)
        db.execute("DELETE FROM key_prompts WHERE id=?", (row["id"],))
        if own:
            filled_fields = {**timer.fields,
                             **{key: values[key] for key in timer.unassigned if values.get(key)},
                             **given}
            filled = replace(
                timer, fields=filled_fields,
                unassigned=tuple(key for key in timer.unassigned if not filled_fields.get(key)),
                # Marks from another key's memory would be kept under this one.
                sources=_person_set({**timer.sources, **(
                    _key_marks(marks, timer.unassigned) if work_key in _own_keys(timer) else {})},
                    None, given),
            )
            _write_timer(db, filled)
            if not filled.unassigned:
                _clear_prompts(db, person, project.workspace_id, _own_keys(filled))
            return _with_finished(Result(UPDATED, timer=filled), finished)
        if timer is not None and timer.mode == MANUAL:
            return _with_finished(Result(REMEMBERED, timer=timer, reasons=(
                f"a manual timer is running on {timer.project_key}; the value is kept for"
                " the next time this starts",)), finished)
        if timer is not None:
            finished = self._finish(db, timer, end=self._end_at(db, timer, at), stop="switch",
                                    settings=settings)
        if paused:
            db.execute("DELETE FROM pauses WHERE person=?", (person,))
        new = Timer(
            person=person, workspace_id=project.workspace_id, project_key=project.key,
            start=at, last_activity=at, mode=MANUAL if paused else AUTO, start_trigger="manual",
            handler=project.handler.name,
            client=self._default_client(project) if client is None else client,
            fields=values, sessions=sessions, unassigned=unassigned,
            sources=_person_set(marks, client, given),
            **self._identity(db, person, project, facts, title, cutoff),
        )
        _write_timer(db, new)
        _clear_prompts(db, person, project.workspace_id, _own_keys(new), run=new.identity)
        return _with_finished(Result(STARTED if finished is None else SWITCHED, timer=new), finished)

    def _manual_follows(self, db, person: str, project: _Project, timer: Timer, facts, *,
                        at: datetime, sessions, issue_map: Mapping, cutoff: str,
                        settings: dict) -> Optional[Result]:
        """A manual timer following a run inside its project (D27), or ``None``
        when the activity leaves its run as it is.

        The same table as an automatic timer (:func:`decide_run`) on the
        manual timer's own clock. A split keeps the timer manual (started by
        ``activity``) and never ends it: a run with no value for a required
        field runs unassigned and is prompted for.
        """
        changed = self._same_project_run(db, person, project, timer, facts, at=at,
                                         issue_map=issue_map, cutoff=cutoff, settings=settings)
        if changed is None:
            return None
        decision, updated = changed
        if decision != SWITCHED:
            updated = _follow_title(replace(updated, seen=max(_latest(timer), at),
                                            sessions=_merge_sessions(timer.sessions, sessions)), facts)
            _write_timer(db, updated)
            _clear_prompts(db, person, project.workspace_id, _own_keys(updated),
                           run=updated.identity)
            return Result(decision, timer=updated)
        finished = self._finish(db, timer, end=at, stop="switch", settings=settings)
        values, failing, _unassigned, marks = self._resolve_fields(
            db, person, project, given={}, facts=facts, issue_map=issue_map, cutoff=cutoff)
        new = Timer(
            person=person, workspace_id=project.workspace_id, project_key=project.key,
            start=at, last_activity=at, mode=MANUAL, start_trigger="activity",
            handler=project.handler.name, client=timer.client, fields=values,
            sessions=_merge_sessions((), sessions),
            sources={**marks,
                     **{key: source for key, source in timer.sources.items() if key == _CLIENT}},
            unassigned=tuple(item.key for item in failing),
            **self._identity(db, person, project, facts, None, cutoff),
        )
        _write_timer(db, new)
        warnings = ()
        if failing:
            missing = [{"key": item.key, "label": item.label, "per_issue": item.per_issue}
                       for item in failing]
            warnings = (self._prompt(db, person, project, facts, missing, at),)
        else:
            _clear_prompts(db, person, project.workspace_id, _own_keys(new),
                           run=new.identity)
        return _with_finished(Result(SWITCHED, timer=new, warnings=warnings,
                                     missing=tuple(item.key for item in failing)), finished)

    def _same_project_run(self, db, person: str, project: _Project, timer: Timer, facts, *,
                          at: datetime, issue_map: Mapping, cutoff: str, settings: dict):
        """``(outcome, timer)`` when activity from *facts* changes *timer*'s run, else ``None``.

        The outcome is :data:`ADOPTED`, :data:`TAKEN_OVER` (the timer comes
        back with the new run) or :data:`SWITCHED` (the caller ends *timer*
        and starts the new run). See :func:`decide_run`.
        """
        late = at < _latest(timer)
        if late or facts is None or timetrack_runs.same_run(timer.identity, facts.ref):
            return None
        if project.handler is None:
            return None
        adopting = timer.identity is None
        if adopting:
            given = {key: value for key, value in timer.fields.items() if value}
        else:
            given = {key: value for key, value in timer.fields.items()
                     if value and timer.sources.get(_FIELD + key) == TITLE_PERSON}
        values, failing, _unassigned, marks = self._resolve_fields(
            db, person, project, given={}, facts=facts, issue_map=issue_map, cutoff=cutoff)
        # A field the person cleared stays empty under the new run.
        cleared = () if adopting else [
            key for key, source in timer.sources.items()
            if source == TITLE_PERSON and key.startswith(_FIELD) and not timer.fields.get(key[len(_FIELD):])]
        merged = {key: value for key, value in {**values, **given}.items()
                  if _FIELD + key not in cleared}
        conflict = adopting and any(values.get(key) not in (None, value) for key, value in given.items())
        fits = not conflict and not _failing_fields(project.handler, merged)
        young = (at - timer.start).total_seconds() < settings["min_entry_seconds"]
        decision = decide_run(timer, facts, late=late, young=young, values_fit=fits)
        if decision == SWITCHED:
            return SWITCHED, timer
        keep_title = timer.title_source == TITLE_PERSON
        return decision, replace(
            timer,
            identity=facts.ref, issues=tuple(facts.issues), work_key=facts.work_key,
            title=timer.title if keep_title else facts.title[:MAX_VALUE_CHARS],
            title_source=timer.title_source if keep_title else TITLE_RUN,
            fields=merged, unassigned=(),
            sources={**{mark: source for mark, source in timer.sources.items() if source != FROM_KEY},
                     **_key_marks(marks, set(merged) - set(given))},
        )

    def _identity(self, db, person: str, project: _Project, facts, title: Optional[str],
                  cutoff: str) -> dict:
        """A new timer's run, issues, work key, title and title source (D22)."""
        values = {"identity": None, "issues": (), "work_key": ""}
        if facts is not None:
            values = {"identity": facts.ref, "issues": tuple(facts.issues),
                      "work_key": facts.work_key}
        if title is not None:
            return {**values, "title": title, "title_source": TITLE_PERSON}
        if facts is not None and facts.title:
            return {**values, "title": facts.title[:MAX_VALUE_CHARS], "title_source": TITLE_RUN}
        recent, source = self._default_title(db, person, project.key, cutoff)
        return {**values, "title": recent, "title_source": source}

    def _resolve_fields(self, db, person: str, project: _Project, *, given: Mapping, facts,
                        issue_map: Mapping, cutoff: str) -> tuple:
        """``(values, failing fields, unassigned keys, marks)`` for an automatic start (D25).

        A field not given takes the person's last value for the work key,
        then the issue map's for an issue, then the project's last value unless
        it is ``per_issue``. With no work key a ``per_issue`` field stays
        empty and unassigned rather than holding the start back. *marks* are
        the :attr:`Timer.sources` entries for the values the key's memory supplied.
        """
        handler = project.handler
        values = dict(given)
        keys = _work_keys(facts) if facts is not None else ()
        issue = facts.work_key if facts is not None and facts.issues else None
        unassigned = []
        marks = {}
        for item in handler.fields:
            if item.key in values:
                continue
            value = _key_value(db, person, handler.name, keys, item.key)
            if value is not None:
                marks[_FIELD + item.key] = FROM_KEY
            if value is None and issue:
                value = (issue_map.get(issue) or {}).get(item.key)
            if value is None and not item.per_issue:
                recent = _recent(db, person, project.key, _FIELD + item.key, cutoff)
                value = recent[0] if recent else None
            if value is None and item.per_issue and item.required and facts is None:
                unassigned.append(item.key)
            elif value is not None:
                values[item.key] = value
        failing = [item for item in _failing_fields(handler, values) if item.key not in unassigned]
        return values, failing, tuple(unassigned), marks

    def _prompt(self, db, person: str, project: _Project, facts, missing: list, at: datetime) -> str:
        """Record (or refresh) the prompt for *facts*' work key and return its message."""
        db.execute(
            "INSERT INTO key_prompts (person, workspace_id, project_key, work_key, run, issues,"
            " missing, at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT (person, workspace_id, work_key) DO UPDATE SET"
            " project_key=excluded.project_key, run=excluded.run, issues=excluded.issues,"
            " missing=excluded.missing, at=excluded.at",
            (person, project.workspace_id, project.key,
             facts.work_key if facts is not None else "",
             json.dumps({**facts.ref.to_dict(), "title": facts.title}) if facts is not None else "",
             json.dumps(list(facts.issues) if facts is not None else []),
             json.dumps(missing), _stamp(at)),
        )
        label = ""
        if facts is not None:
            label = issue_label(facts.issues[0]) if facts.issues else facts.title
        return _prompt_message(project.name, label, missing)

    def _facts(self, ref: Optional[SessionRef]):
        """:attr:`_run_facts` of *ref*, ``None`` when it cannot say."""
        if ref is None:
            return None
        try:
            return self._run_facts(ref)
        except Exception:
            return None

    def _close_if_idle(self, db, timer: Optional[Timer], at: datetime, settings: dict):
        """``(timer or None, finished or None)``: an idle automatic timer ends at its last activity.

        That holds whatever ends it, so a late sweep never stretches an entry
        to the next message.
        """
        if timer is None or not is_idle(timer, at, self._grace_minutes(timer.workspace_id, settings)):
            return timer, None
        return None, self._finish(db, timer, end=timer.last_activity, stop="idle", settings=settings)

    @staticmethod
    def _end_at(db, timer: Timer, at: datetime) -> datetime:
        """Where ending *timer* at *at* stamps its entry: an automatic timer
        gets no time while its person's automatic tracking is off."""
        if timer.mode == AUTO and _read_auto_off(db, timer.person) is not None:
            return min(at, timer.last_activity)
        return at

    def _finish(self, db, timer: Timer, *, end: datetime, stop: str, settings: dict) -> _Finished:
        # A run retitled since its last activity still names the entry (D22).
        timer = _follow_title(timer, self._facts(timer.identity))
        entry = Entry(
            id=new_entry_id(at=timer.start), person=timer.person, project=timer.project_key,
            handler=timer.handler, fields=dict(timer.fields), client=timer.client,
            title=timer.title, start=timer.start, end=max(end, timer.start),
            trigger=Trigger(start=timer.start_trigger, stop=stop), sessions=list(timer.sessions),
            issues=list(timer.issues),
        )
        now = _at(self._now())
        db.execute("DELETE FROM timers WHERE person=?", (timer.person,))
        _mark_stop(db, timer.person, entry.end)
        _remember(db, timer.person, timer.project_key, at=entry.end, cutoff=_cutoff(now, settings),
                  client=entry.client, title=entry.title, fields=entry.fields,
                  handler=timer.handler, work_key=timer.work_key,
                  key_fields=_key_fields(db, timer))
        minimum = settings["min_entry_seconds"]
        if entry.duration >= minimum:
            return _Finished(entry, timesheet_store.enqueue_in(db, entry), None)
        reason = f"shorter than the {minimum}-second minimum"
        db.execute(
            "INSERT INTO kept (id, person, workspace_id, project_key, handler, fields, client, title,"
            " start, end, duration, trigger_start, trigger_stop, reason, at, sessions, identity,"
            " issues) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (entry.id, entry.person, timer.workspace_id, entry.project, entry.handler,
             json.dumps(entry.fields), entry.client, entry.title, _stamp(entry.start),
             _stamp(entry.end), entry.duration, entry.trigger.start, entry.trigger.stop,
             reason, _stamp(now), json.dumps([ref.to_dict() for ref in entry.sessions]),
             json.dumps(timer.identity.to_dict()) if timer.identity else "",
             json.dumps(entry.issues)),
        )
        return _Finished(entry, None, reason)

    # --- lookups outside the store transaction ---------------------------

    def _refusal(self, person: str) -> Optional[Result]:
        if not self.config.time_tracking_enabled:
            return Result(REFUSED, reasons=(
                "time tracking is disabled: " + "; ".join(self.config.time_problems),))
        if not is_person_key(person):
            return Result(REFUSED, reasons=(f"{person!r} is not a person key",))
        return None

    def _ended_refusal(self, person: str) -> Optional[Result]:
        """Inside the store transaction: refused once :meth:`stop_running` ended *person*'s tracking."""
        if self._ended_all or person in self._ended:
            return Result(REFUSED, reasons=(f"time tracking has ended for {person}",))
        return None

    def _project(self, workspace_id: str) -> _Project:
        row = projects.get(workspace_id)
        key = projects.project_key(workspace_id)
        settings = row["settings"]
        handler, warning = timesheet_handlers.lookup(self.config, settings.get("time_handler"))
        return _Project(
            workspace_id=workspace_id, key=key, name=row["name"], archived=row["archived"],
            handler_name=settings.get("time_handler") or DEFAULT_HANDLER,
            handler=handler, handler_warning=warning,
            clients=tuple(settings.get("time_clients") or ()),
        )

    def _client(self, project: _Project, client: Optional[str]) -> Optional[str]:
        """*client* as the project spells it, added to the project when it is new."""
        if not client:
            return client
        labels = project.clients
        if client.casefold() not in {label.casefold() for label in labels}:
            labels = projects.add_client_label(project.workspace_id, client)["settings"]["time_clients"]
        return next((label for label in labels if label.casefold() == client.casefold()), client)

    @staticmethod
    def _default_client(project: _Project) -> str:
        return project.clients[0] if project.clients else ""

    @staticmethod
    def _default_title(db, person: str, project_key: str, cutoff: str) -> tuple:
        """``(title, source)`` with no run to name it: the project's most recent title, else empty."""
        recent = _recent(db, person, project_key, _TITLE, cutoff)
        return (recent[0], TITLE_RECENT) if recent else ("", "")

    @staticmethod
    def _grace_minutes(workspace_id: str, settings: dict) -> int:
        try:
            own = projects.get(workspace_id)["settings"].get("time_idle_grace_minutes")
        except projects.ProjectError:
            own = None
        return own or settings["idle_grace_minutes"]


def _given_fields(handler: HandlerDefinition, fields: Optional[Mapping], *, keep_empty: bool):
    """``(values, warnings)``: *fields* as text, keeping only *handler*'s own keys."""
    values: dict = {}
    warnings: list = []
    for key, value in (fields or {}).items():
        key = str(key)
        if handler.get_field(key) is None:
            warnings.append(f"fields.{key}: the {handler.name} handler has no such field; ignored")
            continue
        text = _text(value) or ""
        if text or keep_empty:
            values[key] = text
    return values, warnings


def _unedited_dropped(base: Optional[Mapping], *, client, title, fields) -> tuple:
    """*client*, *title* and *fields* less each value equal to its *base* (D24)."""
    if not base:
        return client, title, fields
    base_fields = base.get("fields") or {}
    if client is not None and client == base.get("client"):
        client = None
    if title is not None and title == base.get("title"):
        title = None
    if fields:
        fields = {key: value for key, value in fields.items()
                  if key not in base_fields or value != base_fields[key]}
    return client, title, fields


def _follow_title(timer: Optional[Timer], facts) -> Optional[Timer]:
    """*timer* with its run's current title, while its title is the run's (D22)."""
    if (timer is None or facts is None or timer.title_source != TITLE_RUN
            or not timetrack_runs.same_run(facts.ref, timer.identity)):
        return timer
    title = facts.title[:MAX_VALUE_CHARS]
    return timer if not title or title == timer.title else replace(timer, title=title)


def _key_fields(db, timer: Timer) -> dict:
    """The field values *timer* stores for its work key when it ends (D25.1).

    Every value a person set. A value the key's memory supplied only while
    the person's latest value across the run's keys is still that value, or
    there is none left (pruned). Never a map or project value.
    """
    keys = _own_keys(timer)
    kept = {}
    for key, value in timer.fields.items():
        source = timer.sources.get(_FIELD + key)
        if source == TITLE_PERSON or (
                source == FROM_KEY
                and _key_value(db, timer.person, timer.handler, keys, key) in (None, value)):
            kept[key] = value
    return kept


def _key_marks(marks: Mapping, fields) -> dict:
    """The entries of *marks* for the field keys in *fields*."""
    return {mark: source for mark, source in marks.items() if mark[len(_FIELD):] in fields}


def _person_set(sources: Mapping, client: Optional[str], fields: Mapping) -> dict:
    """*sources* plus the client and fields a person just gave."""
    marked = dict(sources)
    if client is not None:
        marked[_CLIENT] = TITLE_PERSON
    for key in fields:
        marked[_FIELD + key] = TITLE_PERSON
    return marked


def _prompt_message(project_name: str, label: str, missing) -> str:
    """"widgets #13 needs a Task to track time" (D25); no label for a run-less prompt."""
    name = f"{project_name} {label}" if label else project_name
    return f"{name} needs a {_labels(missing)} to track time"


def _with_finished(result: Result, finished: Optional[_Finished]) -> Result:
    if finished is None:
        return result
    return replace(result, entry=finished.entry, seq=finished.seq, kept=finished.kept)


# ---------------------------------------------------------------------------
# Process-wide tracker, the way the timesheet writer is published
# ---------------------------------------------------------------------------

_TRACKER: Optional[Tracker] = None
_TRACKER_LOCK = threading.Lock()


def register_tracker(tracker: Optional[Tracker]) -> None:
    global _TRACKER
    with _TRACKER_LOCK:
        _TRACKER = tracker


def get_tracker() -> Optional[Tracker]:
    with _TRACKER_LOCK:
        return _TRACKER


def _not_running() -> Result:
    return Result(REFUSED, reasons=("time tracking is not running",))


def start_tracking_time(person: str, workspace_id: str, *, at: datetime, trigger: str,
                        client: Optional[str] = None, title: Optional[str] = None,
                        fields: Optional[Mapping] = None, runs=(), sessions=()) -> Result:
    """:meth:`Tracker.start` on the registered tracker; refused when none is registered."""
    tracker = get_tracker()
    if tracker is None:
        return _not_running()
    return tracker.start(person, workspace_id, at=at, trigger=trigger, client=client,
                         title=title, fields=fields, runs=runs, sessions=sessions)


def stop_tracking_time(person: str, workspace_id: Optional[str], *, at: datetime, trigger: str) -> Result:
    """:meth:`Tracker.stop` on the registered tracker; refused when none is registered."""
    tracker = get_tracker()
    if tracker is None:
        return _not_running()
    return tracker.stop(person, workspace_id, at=at, trigger=trigger)
