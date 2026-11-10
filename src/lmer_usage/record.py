"""The durable per-session usage record, version 1 (``ai-usage/<session>.yaml``).

The run writes it; the daemon reads it back from the work-repo mirror. A
container can push anywhere in the work repo, so a reader validates every
record before summing it: :func:`validate` checks its shape and bounds, and
:func:`key_matches_path` checks that it sits where its key says it belongs.
Cost is never stored here; it is priced at read time.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Mapping, Optional

from . import buckets

VERSION = 1
DIR_NAME = "ai-usage"
FORMS = ("legacy", "schema1", "schema2")
STATUSES = ("running", "ended", "crashed", "unknown")
ROW_STATUSES = ("collected", "not_collected")
LINKED_KINDS = ("issue", "mr", None)
KEY_FIELDS = ("host", "project", "slug", "form", "schema", "session", "uid")

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: A session id names the record file, so it must be a single safe path segment.
_SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TEXT_LIMIT = 500
_MAX_ROWS = 2000
_MAX_DAYS = 3700


def file_name(session: str) -> str:
    if not valid_session(session):
        raise ValueError(f"unsafe session id for a record file name: {session!r}")
    return f"{session}.yaml"


def valid_session(session: object) -> bool:
    return isinstance(session, str) and bool(_SESSION.match(session)) and ".." not in session


def _iso(value) -> Optional[str]:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def build(*, key: Mapping, summary, status: str, final: bool, updated_at,
          partial: bool = False, taskdef: Optional[str] = None,
          target: Optional[str] = None, linked: Optional[Mapping] = None,
          harness: Optional[str] = None, model: Optional[str] = None,
          effort: Optional[str] = None, platform_session: Optional[str] = None,
          extra_rows: Optional[list] = None, extra_days: Optional[Mapping] = None) -> dict:
    """A version-1 record from a :class:`lmer_usage.summarise.Summary`.

    ``extra_rows`` are rows the summary does not hold, such as fan-out
    children's usage; their collected counters are added to the totals.
    ``extra_days`` is those rows' own per-UTC-day split, added to the days, so
    the rollups, which sum days, count them too. It is also kept on its own as
    ``child_days``: the live view reads only the session's own transcripts, so
    a reader comparing it with the record subtracts the children per day.
    """
    rows = summary.row_list()
    for row in summary.not_collected:
        rows.append({"harness": None, "model": None, "agent": None, "status": "not_collected",
                     "reason": str(row.get("reason", ""))[:_TEXT_LIMIT], **buckets.zero()})
    totals = dict(summary.totals)
    days = {day: dict(counters) for day, counters in summary.days.items()}
    child_days: dict = {}
    for day, counters in (extra_days or {}).items():
        buckets.add(days.setdefault(day, buckets.zero()), counters)
        buckets.add(child_days.setdefault(day, buckets.zero()), counters)
    for row in extra_rows or []:
        rows.append(dict(row))
        if row.get("status", "collected") == "collected":
            buckets.add(totals, row)
    return {
        "version": VERSION,
        "key": {name: key.get(name) for name in KEY_FIELDS},
        "taskdef": taskdef,
        "target": target,
        "linked": dict(linked) if linked else {"kind": None, "ref": None},
        "harness": harness,
        "model": model,
        "effort": effort,
        "platform_session": platform_session,
        "status": status,
        "started_at": _iso(summary.started_at),
        "ended_at": _iso(summary.ended_at),
        "active_seconds": summary.active_seconds,
        "models": rows,
        "days": {day: days[day] for day in sorted(days)},
        "child_days": {day: child_days[day] for day in sorted(child_days)},
        "totals": totals,
        "harness_reported_cost": (
            [{"currency": c, "total": round(v, 6)} for c, v in sorted(summary.reported_cost.items())]
            or None
        ),
        "updated_at": _iso(updated_at),
        "final": bool(final),
        "partial": bool(partial),
    }


def _text(value, *, nullable=True) -> bool:
    if value is None:
        return nullable
    return isinstance(value, str) and len(value) <= _TEXT_LIMIT


def _is_day(day: object) -> bool:
    if not isinstance(day, str) or not _DAY.match(day):
        return False
    try:
        date.fromisoformat(day)
    except ValueError:
        return False
    return True


def validate(data: object) -> list:
    """Every reason ``data`` is not a valid version-1 record; empty when it is.

    Total: records come from a repo every container can push to, so any shape
    is answered with reasons and never with an exception.
    """
    try:
        return _validate(data)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        return [f"record unreadable ({type(exc).__name__})"]


def _validate(data: object) -> list:
    if not isinstance(data, Mapping):
        return ["record is not a mapping"]
    errors = []
    if data.get("version") != VERSION:
        return [f"unknown record version {data.get('version')!r}"]
    key = data.get("key")
    if not isinstance(key, Mapping):
        errors.append("key is not a mapping")
    else:
        for name in ("host", "project", "slug"):
            if not isinstance(key.get(name), str) or not key.get(name):
                errors.append(f"key.{name} missing")
        if key.get("form") not in FORMS:
            errors.append(f"key.form {key.get('form')!r} unknown")
        if key.get("schema") not in (None, 1, 2):
            errors.append(f"key.schema {key.get('schema')!r} unknown")
        if not valid_session(key.get("session")):
            errors.append("key.session is not a safe session id")
        if not _text(key.get("uid")):
            errors.append("key.uid invalid")
    if data.get("status") not in STATUSES:
        errors.append(f"status {data.get('status')!r} unknown")
    for name in ("final", "partial"):
        if not isinstance(data.get(name, False), bool):
            errors.append(f"{name} is not a boolean")
    for name in ("taskdef", "target", "harness", "model", "effort", "platform_session",
                 "started_at", "ended_at", "updated_at"):
        if not _text(data.get(name)):
            errors.append(f"{name} invalid")
    if buckets.count(data.get("active_seconds")) is None:
        errors.append("active_seconds invalid")
    totals = data.get("totals")
    if not buckets.valid(totals):
        errors.append("totals invalid")
    rows = data.get("models")
    summed = buckets.zero()
    if not isinstance(rows, list) or len(rows) > _MAX_ROWS:
        errors.append("models is not a list of rows")
        rows = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping) or not buckets.valid(row):
            errors.append(f"models[{index}] invalid")
            continue
        status = row.get("status", "collected")
        if status not in ROW_STATUSES:
            errors.append(f"models[{index}].status {status!r} unknown")
        for name in ("harness", "model", "agent", "child_session", "reason"):
            if not _text(row.get(name)):
                errors.append(f"models[{index}].{name} invalid")
        if status == "collected":
            buckets.add(summed, row)
        elif not buckets.is_zero(row):
            errors.append(f"models[{index}] is not_collected but carries counts")
    if not errors and isinstance(totals, Mapping):
        if any(int(totals.get(n, 0)) != summed[n] for n in buckets.COUNTERS):
            errors.append("totals do not equal the sum of collected rows")
    days = data.get("days")
    if not isinstance(days, Mapping) or len(days) > _MAX_DAYS:
        errors.append("days is not a mapping")
    else:
        for day, counters in days.items():
            if not _is_day(day) or not buckets.valid(counters):
                errors.append(f"days[{day!r}] invalid")
                continue
            if buckets.valid(totals) and any(
                int(counters.get(n, 0)) > int(totals.get(n, 0)) for n in buckets.COUNTERS
            ):
                errors.append(f"days[{day!r}] exceeds totals")
    child_days = data.get("child_days", {})
    if not isinstance(child_days, Mapping) or len(child_days) > _MAX_DAYS:
        errors.append("child_days is not a mapping")
    else:
        for day, counters in child_days.items():
            if not _is_day(day) or not buckets.valid(counters):
                errors.append(f"child_days[{day!r}] invalid")
                continue
            combined = days.get(day) if isinstance(days, Mapping) else None
            if not buckets.valid(combined) or any(
                int(counters.get(n, 0)) > int(combined.get(n, 0)) for n in buckets.COUNTERS
            ):
                errors.append(f"child_days[{day!r}] exceeds days")
    cost = data.get("harness_reported_cost")
    if cost is not None and not (
        isinstance(cost, list) and all(
            isinstance(c, Mapping) and _text(c.get("currency"), nullable=False)
            and isinstance(c.get("total"), (int, float)) and not isinstance(c.get("total"), bool)
            and c.get("total") >= 0
            for c in cost
        )
    ):
        errors.append("harness_reported_cost invalid")
    return errors


def own_days(data: Mapping) -> dict:
    """A record's per-day split of the session's own usage: ``days`` less its
    fan-out children's ``child_days``. A record written before ``child_days``
    existed has none to subtract. Entries that are not counters are skipped, and
    a day never goes below zero, so an unvalidated record can be read too."""
    def counters(value) -> dict:
        return {n: buckets.count(value.get(n, 0)) or 0 for n in buckets.COUNTERS} \
            if isinstance(value, Mapping) else buckets.zero()
    days, children = data.get("days"), data.get("child_days")
    days = days if isinstance(days, Mapping) else {}
    children = children if isinstance(children, Mapping) else {}
    own = {}
    for day, value in days.items():
        if isinstance(value, Mapping):
            whole, child = counters(value), counters(children.get(day))
            own[day] = {n: max(0, whole[n] - child[n]) for n in buckets.COUNTERS}
    return own


def key_matches_path(data: Mapping, *, host: str, project: str, slug: str, session: str) -> bool:
    """Whether a record's own key names the place it was found."""
    key = data.get("key") if isinstance(data, Mapping) else None
    if not isinstance(key, Mapping):
        return False
    return (key.get("host"), key.get("project"), key.get("slug"), key.get("session")) == (
        host, project, slug, session,
    )
