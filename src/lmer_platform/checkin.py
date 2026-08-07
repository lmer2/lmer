"""Check-ins: the platform notices that *nobody has looked* (issue #244).

Every other digest the assistant gets is driven by an event — a question, a crash,
a completion, a signalled milestone. A run that stops moving emits none of them, so
it was never revisited; in a turn-based flow that idles both sides at once. On
2026-08-05 that cost a review run four silent hours with its fix already pushed.

**Checked** means a call carrying the assistant's own credential and naming one
run. Not ``GET /api/state``: counting the fleet-wide read would mark everything
checked at once, which is the blindness this ends. An unattributed read is no read
— the cost is a digest about a run somebody did look at, never silence about one
nobody did.

**Three stamps** per run — ``checked_at``, ``announced_at``, ``first_seen`` — and
staleness runs from the latest. ``announced_at`` is kept apart so the digest cannot
read as a check-in; ``first_seen`` keeps a daemon restart from turning a long
history into a spool full of digests.

**One note names every stale run**, because a quiet fleet is exactly when several
go stale together. Its class is :data:`STALE_DIGEST_KIND` and pointedly not an
:data:`lmer_platform.inventory.ATTENTION_REASONS` member: that axis means a *person*
must act, this means the orchestrator has a next step
(:data:`lmer_platform.detect.SIGNAL_DIGEST_KIND` stands beside it for the same
reason).

Reads never raise; writes do, because a stale run has no second copy the way a
signal has ``events.jsonl``. The retry belongs to the *window*, not the tick — at
30s a tick, an unwritable marks file evicted every other digest class from a
50-note spool within half an hour (:meth:`lmer_platform.detect.Detector._check_ins`
holds the announcement in memory instead).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .assistant import MAX_NOTE_CHARS
from .store import (
    StoreError,
    age_seconds,
    clamp_text,
    mutating,
    read_json,
    snapshot_path,
    utc_now_iso,
    write_json,
)

logger = logging.getLogger("lmer_platform.checkin")

__all__ = [
    "CHECKIN_MARKS_FILE", "STALE_DIGEST_KIND", "MAX_NAMED",
    "MAX_LABEL_CHARS", "MAX_DATA_LABEL_CHARS", "FUTURE_GRACE_SECONDS", "StaleRun",
    "marks_path", "read_marks", "record_check", "observe", "stale_runs",
    "record_announced", "digest", "checkin_of", "is_eligible", "row_checkin",
]

#: ``{"runs": {"<host>/<project>/<slug>": {"checked_at", "announced_at",
#: "first_seen"}}}``, beside the daemon's other snapshots (spec §6.1).
CHECKIN_MARKS_FILE = "checkins.json"

#: A label on a spooled note, never an attention reason (module docstring).
STALE_DIGEST_KIND = "stale_runs"

#: Runs the note names before it summarises the rest. Small because it is read
#: on a phone, and safe because ``data`` beside it carries every one of them.
MAX_NAMED = 8

#: Longest label the note quotes. Labels are agent-authored and unbounded, and
#: ``notify`` *refuses* an oversized note rather than trimming it — so without
#: this one 3 KB label would end check-ins on the host silently.
MAX_LABEL_CHARS = 60

#: Same for the structured half: generous, because a caller reads it, but not
#: unbounded — one digest carries *N* runs and every read of ``assistant.json``
#: re-parses and re-scrubs up to ``MAX_PENDING`` of them.
MAX_DATA_LABEL_CHARS = 200

#: How far ahead of now a stamp may sit and still be believed. One writer, one
#: wall clock, so anything further is skew — and believing it costs the run its
#: reminders until that date arrives.
FUTURE_GRACE_SECONDS = 60

#: States nobody owes a turn on (an ``archived`` run is not in the view at all).
#: Names from :data:`lmer_platform.inventory.RUN_STATES`.
SETTLED_STATES = ("complete",)

@dataclass(frozen=True)
class StaleRun:
    """One run nobody has checked, and how long that has been true."""

    host: str
    project: str
    slug: str
    label: str
    state: str
    #: When the clock last restarted — a check, a digest, or first sight.
    since: Optional[str]
    age_seconds: Optional[float]

    @property
    def ref(self) -> str:
        return f"{self.host}/{self.project}/{self.slug}"

    @property
    def key(self) -> tuple:
        return (self.host, self.project, self.slug)

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "project": self.project,
            "slug": self.slug,
            "label": clamp_text(self.label, MAX_DATA_LABEL_CHARS),
            "state": self.state,
            "since": self.since,
            "age_seconds": (
                None if self.age_seconds is None else round(self.age_seconds)
            ),
        }


def marks_path() -> Path:
    return snapshot_path(CHECKIN_MARKS_FILE)


def _run_key(host: str, project: str, slug: str) -> str:
    return f"{host}/{project}/{slug}"


def read_marks() -> dict:
    """``{run key: {stamp: iso}}``, or empty when there is no usable file.

    Never raises: empty is the safe answer, since every run then reads as newly
    seen and nothing is announced for a window — a daemon restart's own cost.
    """
    try:
        stored = read_json(marks_path())
    except StoreError as exc:
        logger.warning(
            "platform_checkin_marks_unreadable error=%s — every run reads as newly "
            "seen, so check-in digests resume one window from now", exc,
        )
        return {}
    runs = stored.get("runs") if isinstance(stored, dict) else None
    if not isinstance(runs, dict):
        return {}
    marks = {}
    for ref, record in runs.items():
        if not isinstance(ref, str) or not isinstance(record, dict):
            continue
        kept = {
            field: value
            for field, value in record.items()
            if field in ("checked_at", "announced_at", "first_seen")
            and isinstance(value, str) and value
        }
        if kept:
            marks[ref] = kept
    return marks


def _write_marks(marks: dict) -> None:
    write_json(marks_path(), {"runs": {ref: marks[ref] for ref in sorted(marks)}})


def _stamp(ref: str, field: str, when: str) -> None:
    """Set one stamp on one run, preserving the rest of its record."""
    path = marks_path()
    # Under the lock: the API handlers and the detector write concurrently, and a
    # dropped check is a run the assistant hears about again seconds after reading it.
    with mutating(path):
        marks = read_marks()
        record = dict(marks.get(ref) or {})
        record[field] = when
        record.setdefault("first_seen", when)
        marks[ref] = record
        _write_marks(marks)


def record_check(host: str, project: str, slug: str) -> bool:
    """Stamp a run as checked, now. Returns whether it was written.

    Absorbs its own failure: a stamp that will not write must not turn a
    successful read into a 500, and it costs one superfluous reminder.
    """
    if not (host and project and slug):
        return False
    try:
        _stamp(_run_key(host, project, slug), "checked_at", utc_now_iso())
    except StoreError as exc:
        logger.warning(
            "platform_checkin_unrecorded run=%s/%s/%s error=%s — the read "
            "happened; only the record of it did not",
            host, project, slug, exc,
        )
        return False
    return True


def _rows(payload: object) -> list:
    rows = payload.get("runs") if isinstance(payload, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _identity(row: dict) -> Optional[tuple]:
    host, project, slug = row.get("host"), row.get("project"), row.get("slug")
    if not all(isinstance(value, str) and value for value in (host, project, slug)):
        return None
    return (host, project, slug)


def is_eligible(row: dict) -> bool:
    """Whether a check-in is owed on this row. See the module docstring.

    Public because the fleet view asks it too: a second reading of it is how a
    badge starts marking a run no digest will ever name.
    """
    if row.get("orchestrator"):
        return False
    return row.get("state") not in SETTLED_STATES


def observe(payload: object) -> dict:
    """Record first sight of runs never seen, drop marks for runs that are gone.

    Returns the marks as they stand, so one tick reads the file once. The prune
    lives here rather than on the stamping path because this is the only caller
    holding the fleet view that authorises dropping a run.
    """
    rows = _rows(payload)
    present = {
        _run_key(*identity)
        for identity in (_identity(row) for row in rows)
        if identity is not None
    }
    marks = read_marks()
    unseen = present - set(marks)
    departed = set(marks) - present
    if not unseen and not departed:
        return marks

    now = utc_now_iso()
    with mutating(marks_path()):
        marks = read_marks()
        for ref in present - set(marks):
            marks[ref] = {"first_seen": now}
        for ref in set(marks) - present:
            marks.pop(ref, None)
        _write_marks(marks)
    if unseen:
        logger.debug(
            "platform_checkin_baseline runs=%d — newly seen runs are not stale "
            "until a window has passed", len(unseen),
        )
    return marks


def _latest_stamp(mark: dict, *, now=None) -> Optional[str]:
    """The latest *usable* of a run's three stamps — when its clock last restarted.

    Parsing before comparing is the whole function. Picking the lexical ``max``
    first let one bad value capture the answer forever: ``"yesterday afternoon"``
    outsorts every real timestamp and parses as nothing, so the run read as "not
    stale" with a correct ``checked_at`` sitting right beside it, and no later
    check could clear it. A future-dated stamp did the same until that date came,
    which NTP correcting a skewed boot is enough to produce.

    So all-unusable reads as never stamped — recoverable, because the next real
    check beats it.
    """
    best_stamp, best_age = None, None
    for value in mark.values():
        if not isinstance(value, str) or not value:
            continue
        age = age_seconds(value, now=now)
        if age is None or age < -FUTURE_GRACE_SECONDS:
            continue
        if best_age is None or age < best_age:
            best_stamp, best_age = value, age
    return best_stamp


def checkin_of(mark: Optional[dict], *, window: int, now=None) -> dict:
    """One run's check-in facts, for a fleet row or a staleness decision.

    ``checked_at`` answers "when did the assistant last look" and is ``None`` when
    it never has; ``since`` is what the window is measured from, which also counts
    a digest already sent and the run's first sighting.
    """
    mark = mark if isinstance(mark, dict) else {}
    since = _latest_stamp(mark, now=now)
    age = age_seconds(since, now=now)
    stale = bool(window) and age is not None and age >= window
    return {
        "checked_at": mark.get("checked_at"),
        "since": since,
        "age_seconds": None if age is None else round(age),
        "stale": stale,
    }


def row_checkin(row: dict, marks: dict, *, window: int, now=None) -> dict:
    """One fleet row's check-in block: what the operator sees on the run.

    Through the same two functions the digest uses, because a badge and a digest
    that disagree about which runs went quiet is worse than either alone.

    A row that cannot go stale still reports its age — true and harmless — but
    never the marker, which would point at a reminder that will never come.
    """
    identity = _identity(row)
    mark = marks.get(_run_key(*identity)) if identity else None
    facts = checkin_of(mark, window=window, now=now)
    if not is_eligible(row):
        facts["stale"] = False
    return facts


def stale_runs(payload: object, *, window: int, marks: Optional[dict] = None) -> list:
    """Runs past the window, oldest first. Empty when *window* is 0 (disabled).

    A pure read, so a caller that dies before :func:`record_announced` finds the
    same runs next tick instead of losing them (deliver-then-mark).
    """
    if not window:
        return []
    marks = read_marks() if marks is None else marks
    now = datetime.now(timezone.utc)
    found = []
    for row in _rows(payload):
        identity = _identity(row)
        if identity is None or not is_eligible(row):
            continue
        facts = checkin_of(marks.get(_run_key(*identity)), window=window, now=now)
        if not facts["stale"]:
            continue
        host, project, slug = identity
        label = row.get("label")
        found.append(StaleRun(
            host=host,
            project=project,
            slug=slug,
            label=label if isinstance(label, str) and label else slug,
            state=row.get("state") if isinstance(row.get("state"), str) else "unknown",
            since=facts["since"],
            age_seconds=facts["age_seconds"],
        ))
    return sorted(found, key=lambda run: (-(run.age_seconds or 0), run.ref))


def record_announced(runs: list) -> None:
    """Stamp every run in *runs* as announced, now.

    What makes a stale run cost one digest per window rather than one per tick.
    Written *after* delivery: a crash between the two costs a duplicate the
    assistant can recognise, where the other order loses the announcement.
    """
    if not runs:
        return
    now = utc_now_iso()
    with mutating(marks_path()):
        marks = read_marks()
        for run in runs:
            record = dict(marks.get(run.ref) or {})
            record["announced_at"] = now
            record.setdefault("first_seen", now)
            marks[run.ref] = record
        _write_marks(marks)


def _duration(seconds: Optional[float]) -> str:
    """``4h20m`` / ``35m`` / ``2d3h`` — an age a person reads at a glance."""
    if seconds is None:
        return "an unknown time"
    total = int(max(seconds, 0))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d{hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def digest(runs: list, *, window: int, caveat: Optional[str] = None) -> tuple:
    """``(note, data)`` for one check-in digest naming every run in *runs*.

    The note names at most :data:`MAX_NAMED` runs and counts the rest; ``data``
    names all of them, so the assistant never parses prose to act.

    *caveat* says something about the *mechanism* rather than about a run — used
    when the digest is knowably not going to stop
    (:meth:`lmer_platform.detect.Detector._check_ins`). Composed after the list is
    clamped, so the sentence explaining the digest cannot be what gets cut.
    """
    named = runs[:MAX_NAMED]
    described = ", ".join(
        f"{clamp_text(run.label, MAX_LABEL_CHARS)} ({_duration(run.age_seconds)})"
        for run in named
    )
    if len(runs) > len(named):
        described = f"{described} (+{len(runs) - len(named)} more)"
    subject = "run has" if len(runs) == 1 else "runs have"
    note = (
        f"{len(runs)} {subject} gone unchecked for over {_duration(window)}: "
        f"{described} — read each one, and if a turn is owed take it."
    )
    # A backstop: MAX_LABEL_CHARS is what actually keeps this short, and this
    # holds if a later edit adds something unbounded beside it. Enforced rather
    # than asserted in prose because notify() REFUSES an over-long note and a
    # refused digest retries forever.
    note = clamp_text(note, MAX_NOTE_CHARS - (len(caveat) + 1 if caveat else 0))
    if caveat:
        note = f"{note} {caveat}"
    payload = {
        "window_seconds": window,
        "count": len(runs),
        "runs": [run.to_dict() for run in runs],
    }
    if caveat:
        payload["caveat"] = caveat
    return note, payload
