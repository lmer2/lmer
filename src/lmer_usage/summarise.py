"""Summarise one session's transcripts into usage, from scratch.

``summarise(files)`` re-parses every file and keeps no state, so running it
twice gives the same answer. Records are dispatched by their own shape
(:mod:`lmer_usage.shape`), never by what a caller believes wrote the file.
Deduplication is per session across all of its files: a claude resume or a pi
fork that copies history into a new file still counts each response once.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Optional

from . import buckets, claude, codex, pi, shape
from .timestamps import parse_timestamp

#: Gaps between consecutive records up to this many seconds count as active
#: time; a longer gap (blocked on a question, idle overnight) does not.
DEFAULT_IDLE_SECONDS = 300
IDLE_ENV = "LMER_USAGE_IDLE_SECONDS"
#: Lines longer than this are skipped, as the transcript view does.
MAX_LINE_BYTES = 8 * 1024 * 1024

ADAPTERS: dict = {
    claude.HARNESS: claude.Adapter(),
    codex.HARNESS: codex.Adapter(),
    pi.HARNESS: pi.Adapter(),
}

_NO_USAGE_FORMATS = {
    shape.LMER_HARNESS: "drop-in harness transcript (lmer format) carries no usage",
}


def register(adapter) -> None:
    """Make ``adapter`` the reader for records of its harness."""
    ADAPTERS[adapter.harness] = adapter


def idle_seconds_from_env(environ=os.environ) -> int:
    raw = (environ.get(IDLE_ENV) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_IDLE_SECONDS
    return value if value > 0 else DEFAULT_IDLE_SECONDS


@dataclass
class Summary:
    """One session's usage: rows per (harness, model, agent), a per-UTC-day split
    and the wall clock. ``not_collected`` lists files nobody could count."""

    rows: dict = field(default_factory=dict)
    days: dict = field(default_factory=dict)
    totals: dict = field(default_factory=buckets.zero)
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    active_seconds: int = 0
    harnesses: set = field(default_factory=set)
    not_collected: list = field(default_factory=list)
    reported_cost: dict = field(default_factory=dict)

    def add_event(self, event: buckets.Event) -> None:
        key = (event.harness, event.model, event.agent)
        buckets.add(self.rows.setdefault(key, buckets.zero()), event.counters)
        buckets.add(self.totals, event.counters)
        self.harnesses.add(event.harness)
        if event.at is not None:
            day = event.at.date().isoformat()
            buckets.add(self.days.setdefault(day, buckets.zero()), event.counters)
        if event.reported_cost:
            currency, amount = event.reported_cost
            self.reported_cost[currency] = self.reported_cost.get(currency, 0.0) + amount

    def row_list(self) -> list:
        """Rows as dicts, in a stable order."""
        out = []
        for (harness, model, agent), counters in sorted(
            self.rows.items(), key=lambda item: tuple("" if v is None else str(v) for v in item[0])
        ):
            out.append({"harness": harness, "model": model, "agent": agent,
                        "status": "collected", **counters})
        return out


def open_regular(path: Path):
    """Open ``path`` for binary reading, refusing symlinks, FIFOs and devices."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular file")
        os.set_blocking(fd, True)
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


def iter_records(handle) -> Iterator[dict]:
    """JSON objects from a JSONL stream, skipping bad, partial and oversized lines."""
    for line in handle:
        if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
            continue
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(record, dict):
            yield record


def active_seconds(stamps: Iterable[datetime], idle_seconds: int) -> int:
    ordered = sorted(stamps)
    total = 0.0
    for earlier, later in zip(ordered, ordered[1:]):
        gap = (later - earlier).total_seconds()
        if 0 < gap <= idle_seconds:
            total += gap
    return int(total)


def summarise(files: Iterable, *, idle_seconds: Optional[int] = None) -> Summary:
    """Usage for one session from all of its transcript files."""
    idle = idle_seconds_from_env() if idle_seconds is None else idle_seconds
    events: dict = {}
    stamps: list = []
    summary = Summary()
    for path in files:
        path = Path(path)
        seen: set = set()
        try:
            handle = open_regular(path)
        except OSError as exc:
            summary.not_collected.append({"file": str(path), "reason": f"unreadable: {exc.strerror or exc}"})
            continue
        with handle:
            states: dict = {}
            for record in iter_records(handle):
                harness = shape.harness_of_record(record)
                if harness is None:
                    continue
                seen.add(harness)
                parsed = parse_timestamp(record.get("timestamp"))
                if parsed is not None:
                    stamps.append(parsed)
                adapter = ADAPTERS.get(harness)
                if adapter is None:
                    continue
                if harness not in states:
                    states[harness] = adapter.start_file(path)
                for event in adapter.events(record, states[harness]):
                    if event.at is None:
                        event.at = parsed
                    events[event.key] = event
        for harness in sorted(seen - set(ADAPTERS)):
            reason = _NO_USAGE_FORMATS.get(harness, f"no usage adapter for {harness}")
            summary.not_collected.append({"file": str(path), "reason": reason})
    for event in events.values():
        summary.add_event(event)
    if stamps:
        summary.started_at = min(stamps)
        summary.ended_at = max(stamps)
        summary.active_seconds = active_seconds(stamps, idle)
    return summary
