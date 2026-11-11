"""The sync ledger contract (spec D20).

A handler's sync process runs outside lmer and appends one JSON line per
entry it handled to ``timesheet.d/<person>/.sync/<handler>.jsonl``::

    {"id": ..., "hash": "sha256:...", "status": "synced"|"held",
     "ref": ..., "link": ..., "reason": ..., "at": "<ISO 8601>"}

``hash`` is :func:`entry_hash` of the entry file's raw bytes at sync time, so
an edited file no longer matches and shows as ``pending`` again.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

from lmer_timesheet.entry import (
    TIMESHEET_DIR,
    Entry,
    format_timestamp,
    is_person_key,
    is_slug,
    parse_timestamp,
)
from lmer_timesheet.handlers import is_http_url

__all__ = [
    "SYNC_STATUSES",
    "LedgerLine",
    "parse_ledger",
    "ledger_path",
    "entry_hash",
    "sync_status",
]

SYNC_STATUSES = ("synced", "held")


@dataclass(frozen=True, kw_only=True)
class LedgerLine:
    """One ledger record: what a sync process did with one version of an entry."""

    id: str
    hash: str
    status: str
    ref: str | None = None
    link: str | None = None
    reason: str | None = None
    at: datetime

    def to_json(self) -> str:
        """The line as written to the ledger, without the trailing newline."""
        data = {"id": self.id, "hash": self.hash, "status": self.status}
        for key in ("ref", "link", "reason"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        data["at"] = format_timestamp(self.at)
        return json.dumps(data, ensure_ascii=False)


def entry_hash(raw: bytes) -> str:
    """``sha256:<hex>`` of an entry file's bytes, exactly as stored."""
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def ledger_path(person: str, handler: str) -> PurePosixPath:
    """*person*'s ledger for *handler*, relative to the work-repo root."""
    if not is_person_key(person):
        raise ValueError(f"person key {person!r} is not a valid person key")
    if not is_slug(handler):
        raise ValueError(f"handler {handler!r} is not a handler name")
    return PurePosixPath(TIMESHEET_DIR, person, ".sync", f"{handler}.jsonl")


def parse_ledger(text: str) -> tuple[list[LedgerLine], list[str]]:
    """Parse a ledger file. Returns ``(lines, problems)``.

    A bad line is skipped and reported by its 1-based line number; blank
    lines are ignored. Lines keep their file order. Only a newline ends a
    line: JSON strings may hold U+2028 and the other separators
    ``str.splitlines`` also breaks on.
    """
    lines: list[LedgerLine] = []
    problems: list[str] = []
    for number, raw in enumerate(text.split("\n"), start=1):
        if not raw.strip():
            continue
        line, problem = _parse_line(raw)
        if line is None:
            problems.append(f"line {number}: {problem}")
        else:
            lines.append(line)
    return lines, problems


def _parse_line(raw: str) -> tuple[LedgerLine | None, str | None]:
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        return None, f"not JSON: {exc}"
    if not isinstance(data, dict):
        return None, "not a JSON object"
    for key in ("id", "hash", "status", "at"):
        if not isinstance(data.get(key), str) or not data[key]:
            return None, f"{key}: required string"
    if data["status"] not in SYNC_STATUSES:
        return None, f"status: {data['status']!r} is not one of {SYNC_STATUSES}"
    for key in ("ref", "link", "reason"):
        if data.get(key) is not None and not isinstance(data[key], str):
            return None, f"{key}: must be a string"
    if data.get("link") is not None and not is_http_url(data["link"]):
        return None, "link: must be an http(s) URL"
    try:
        at = parse_timestamp(data["at"])
    except ValueError as exc:
        return None, f"at: {exc}"
    return (
        LedgerLine(
            id=data["id"],
            hash=data["hash"],
            status=data["status"],
            ref=data.get("ref"),
            link=data.get("link"),
            reason=data.get("reason"),
            at=at,
        ),
        None,
    )


def sync_status(
    entry: Entry | str, ledger_lines, *, raw: bytes
) -> tuple[str, LedgerLine | None]:
    """``("synced"|"held"|"pending", line)`` for one entry file.

    *entry* is the parsed entry or its id, *raw* the file's current bytes.
    The last ledger line for the id wins. With no line the status is
    ``pending`` and the line ``None``; when that line's hash differs from
    *raw*'s, the status is ``pending`` and the stale line is returned, so
    the caller can still link the external entry.
    """
    entry_id = entry if isinstance(entry, str) else entry.id
    latest = None
    for line in ledger_lines:
        if line.id == entry_id:
            latest = line
    if latest is None:
        return "pending", None
    if latest.hash != entry_hash(raw):
        return "pending", latest
    return latest.status, latest
