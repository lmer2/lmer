"""One live session's usage, read incrementally (#315, #647).

:func:`lmer_usage.summarise.summarise` re-reads every file on every call. The
platform daemon polls every 30 seconds, so :class:`IncrementalReader` reads only
the bytes appended since its last call and persists its state as JSON across
restarts. It opens files, caps lines and dispatches records exactly as
``summarise`` does, so the same bytes give the same numbers.

A file is re-read from 0 with fresh adapter states when its device or inode
changes, or when it is shorter than the reader's offset. Session-wide dedup
makes that re-read idempotent. A rewrite in place that leaves the file at least
as long as the offset cannot be detected, and is read as an append.

Wall-clock time is kept as spans, not as every timestamp: a span is a run of
records no further apart than the idle threshold, which is exactly what
``active_seconds`` adds up, so the state stays small for a long session. The
threshold is fixed when the first timestamp is read.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from . import buckets, shape
# By name: the package rebinds ``lmer_usage.summarise`` to the function.
from .summarise import (
    _NO_USAGE_FORMATS, ADAPTERS, Summary, idle_seconds_from_env,
    iter_records, open_regular,
)
from .timestamps import parse_timestamp

VERSION = 2


@dataclass
class _Cursor:
    """How far one file has been read, and what reading it has produced."""

    dev: int = 0
    inode: int = 0
    size: int = 0
    offset: int = 0
    states: dict = field(default_factory=dict)
    seen: set = field(default_factory=set)


class _BadState(ValueError):
    pass


def _complete_lines(handle, cursor: _Cursor):
    """``handle``'s ``\\n``-terminated lines, advancing ``cursor.offset`` past
    each. A partial last line is left for the next read."""
    for line in handle:
        if not line.endswith(b"\n"):
            return
        cursor.offset += len(line)
        yield line


def _fields(event: buckets.Event) -> tuple:
    return tuple(getattr(event, name) for name in buckets.Event.__slots__)


class IncrementalReader:
    """Usage for ONE session, from the bytes appended since the last update."""

    def __init__(self, idle_seconds: Optional[int] = None):
        self._idle = idle_seconds
        self._files: dict = {}
        # Every event ever seen, by its session-wide key; the last one wins.
        self._events: dict = {}
        self._not_collected: list = []
        # Sorted, disjoint [start, end] epoch seconds, more than the idle
        # threshold apart; see the module docstring.
        self._spans: list = []
        self._span_idle: Optional[int] = None
        #: Bumped whenever the summary changes: an event is new or differs from
        #: the one held under its key, or the activity spans move. Re-reading
        #: the same records (a rewritten file) leaves it alone.
        self.revision = 0

    def update(self, files: Iterable) -> list:
        """Read what was appended to ``files`` and return the events whose key
        this reader had never seen. A key's later records update the stored
        event but are not returned again."""
        fresh: dict = {}
        # Each touched key's fields before this update: a response is often
        # written as several records, so only the end state is compared.
        touched: dict = {}
        not_collected: list = []
        for path in files:
            path = Path(path)
            name = str(path)
            try:
                handle = open_regular(path)
            except OSError as exc:
                not_collected.append({"file": name, "reason": f"unreadable: {exc.strerror or exc}"})
                continue
            try:
                with handle:
                    info = os.fstat(handle.fileno())
                    cursor = self._files.get(name)
                    if (cursor is None or (cursor.dev, cursor.inode) != (info.st_dev, info.st_ino)
                            or info.st_size < cursor.offset):
                        cursor = _Cursor(dev=info.st_dev, inode=info.st_ino)
                        self._files[name] = cursor
                    cursor.size = info.st_size
                    if info.st_size > cursor.offset:
                        handle.seek(cursor.offset)
                        self._read(path, handle, cursor, fresh, touched)
            except OSError as exc:
                not_collected.append({"file": name, "reason": f"unreadable: {exc.strerror or exc}"})
                continue
            for harness in sorted(cursor.seen - set(ADAPTERS)):
                reason = _NO_USAGE_FORMATS.get(harness, f"no usage adapter for {harness}")
                not_collected.append({"file": name, "reason": reason})
        self._not_collected = not_collected
        if any(was != _fields(self._events[key]) for key, was in touched.items()):
            self.revision += 1
        return [self._events[key] for key in fresh]

    def _read(self, path: Path, handle, cursor: _Cursor, fresh: dict, touched: dict) -> None:
        # Mirrors summarise()'s per-record loop, which offers no per-record hook.
        for record in iter_records(_complete_lines(handle, cursor)):
            harness = shape.harness_of_record(record)
            if harness is None:
                continue
            cursor.seen.add(harness)
            parsed = parse_timestamp(record.get("timestamp"))
            if parsed is not None:
                self._add_stamp(parsed.timestamp())
            adapter = ADAPTERS.get(harness)
            if adapter is None:
                continue
            if harness not in cursor.states:
                cursor.states[harness] = adapter.start_file(path)
            for event in adapter.events(record, cursor.states[harness]):
                if event.at is None:
                    event.at = parsed
                held = self._events.get(event.key)
                if held is None:
                    fresh[event.key] = None
                if event.key not in touched:
                    touched[event.key] = None if held is None else _fields(held)
                self._events[event.key] = event

    def _add_stamp(self, at: float) -> None:
        if self._span_idle is None:
            self._span_idle = idle_seconds_from_env() if self._idle is None else self._idle
        idle, spans = self._span_idle, self._spans
        index = len(spans)
        while index and spans[index - 1][0] - idle > at:
            index -= 1
        # spans[index:] start too late to take ``at``; merge what reaches it.
        low = index
        while low and spans[low - 1][1] + idle >= at:
            low -= 1
        merged = [at, at]
        for span in spans[low:index]:
            merged = [min(merged[0], span[0]), max(merged[1], span[1])]
        if spans[low:index] != [merged]:
            spans[low:index] = [merged]
            # The spans are part of the summary (started, ended, active), so a
            # record carrying no usage still changes what a reader publishes.
            self.revision += 1

    @property
    def not_collected(self) -> list:
        """The files the last update could not count, and why."""
        return [dict(entry) for entry in self._not_collected]

    def events(self) -> list:
        """Every response seen so far, one per key, each with its own ``at``."""
        return list(self._events.values())

    def summary(self) -> Summary:
        """The session's usage so far, as ``summarise`` would report it."""
        out = Summary(not_collected=[dict(entry) for entry in self._not_collected])
        for event in self._events.values():
            out.add_event(event)
        if self._spans:
            out.started_at = datetime.fromtimestamp(self._spans[0][0], timezone.utc)
            out.ended_at = datetime.fromtimestamp(self._spans[-1][1], timezone.utc)
            out.active_seconds = int(sum(end - start for start, end in self._spans))
        return out

    def to_json(self) -> dict:
        """The reader's state as JSON-safe values; tuples become lists."""
        return {
            "version": VERSION,
            "idle_seconds": self._idle,
            "files": {
                name: {
                    "dev": c.dev, "inode": c.inode, "size": c.size, "offset": c.offset,
                    "states": {h: _plain(s) for h, s in c.states.items()},
                    "seen": sorted(c.seen),
                }
                for name, c in self._files.items()
            },
            "events": [
                {
                    "key": _plain(e.key), "harness": e.harness, "model": e.model, "agent": e.agent,
                    "at": None if e.at is None else e.at.isoformat(),
                    "counters": dict(e.counters),
                    "reported_cost": None if e.reported_cost is None else list(e.reported_cost),
                }
                for e in self._events.values()
            ],
            "not_collected": [dict(entry) for entry in self._not_collected],
            "spans": [list(span) for span in self._spans],
            "span_idle": self._span_idle,
        }

    @classmethod
    def from_json(cls, data) -> "IncrementalReader":
        """A reader restored from :meth:`to_json`, or a fresh one when ``data``
        is malformed, foreign or another version. Never raises: a daemon with
        bad state starts over rather than stopping."""
        try:
            return cls._load(data)
        except Exception:  # noqa: BLE001 - any failure means "start over"
            return cls()

    @classmethod
    def _load(cls, data) -> "IncrementalReader":
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != VERSION:
            raise _BadState(f"not version {VERSION} state")
        idle = data.get("idle_seconds")
        if idle is not None and not _is_count(idle):
            raise _BadState("idle_seconds")
        reader = cls(idle_seconds=idle)
        files = _expect(data.get("files", {}), dict)
        for name, raw in files.items():
            reader._files[_expect(name, str)] = _load_cursor(name, _expect(raw, dict))
        for raw in _expect(data.get("events", []), list):
            event = _load_event(_expect(raw, dict))
            reader._events[event.key] = event
        for raw in _expect(data.get("not_collected", []), list):
            entry = _expect(raw, dict)
            if not all(isinstance(entry.get(k), str) for k in ("file", "reason")):
                raise _BadState("not_collected")
            reader._not_collected.append({"file": entry["file"], "reason": entry["reason"]})
        span_idle = data.get("span_idle")
        if span_idle is not None and not _is_count(span_idle):
            raise _BadState("span_idle")
        reader._span_idle = span_idle
        previous = None
        for raw in _expect(data.get("spans", []), list):
            start, end = _expect(raw, list)
            if (any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (start, end))
                    or end < start or (previous is not None and start <= previous)):
                raise _BadState("spans")
            reader._spans.append([float(start), float(end)])
            previous = end
        return reader


def _plain(value):
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _restore(value):
    """JSON lists back to tuples, at any depth. Adapter states and keys hold
    tuples, never lists, so the mapping is exact for them."""
    if isinstance(value, list):
        return tuple(_restore(v) for v in value)
    if isinstance(value, dict):
        return {_expect(k, str): _restore(v) for k, v in value.items()}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise _BadState(f"unexpected {type(value).__name__}")


def _expect(value, kind):
    if not isinstance(value, kind):
        raise _BadState(f"expected {kind.__name__}")
    return value


def _is_count(value) -> bool:
    return type(value) is int and value >= 0


def _load_stamp(value) -> datetime:
    stamp = datetime.fromisoformat(_expect(value, str))
    if stamp.tzinfo is None:
        raise _BadState("naive timestamp")
    return stamp


def _load_cursor(name: str, raw: dict) -> _Cursor:
    numbers = [raw.get(k) for k in ("dev", "inode", "size", "offset")]
    if not all(_is_count(n) for n in numbers):
        raise _BadState("cursor")
    states = {}
    for harness, state in _expect(raw.get("states", {}), dict).items():
        adapter = ADAPTERS.get(harness)
        state = _restore(_expect(state, dict))
        # Only the key set is checked: whether a value suits its adapter (a
        # codex ``previous`` of the right length, say) is not knowable here.
        if adapter is None or set(state) != set(adapter.start_file(Path(name))):
            raise _BadState(f"state for {harness}")
        states[harness] = state
    seen = {_expect(h, str) for h in _expect(raw.get("seen", []), list)}
    dev, inode, size, offset = numbers
    return _Cursor(dev=dev, inode=inode, size=size, offset=offset, states=states, seen=seen)


def _load_event(raw: dict) -> buckets.Event:
    key = _restore(_expect(raw.get("key"), list))
    hash(key)
    counters = _expect(raw.get("counters"), dict)
    if set(counters) != set(buckets.COUNTERS) or not buckets.valid(counters):
        raise _BadState("counters")
    harness, model, agent = raw.get("harness"), raw.get("model"), raw.get("agent")
    if not isinstance(harness, str) or not isinstance(model, str) or not (agent is None or isinstance(agent, str)):
        raise _BadState("event labels")
    cost = raw.get("reported_cost")
    if cost is not None:
        currency, amount = _expect(cost, list)
        if (not isinstance(currency, str) or isinstance(amount, bool)
                or not isinstance(amount, (int, float)) or amount < 0):
            raise _BadState("reported_cost")
        cost = (currency, float(amount))
    at = raw.get("at")
    return buckets.Event(
        key=key, harness=harness, model=model, agent=agent,
        at=None if at is None else _load_stamp(at),
        counters={name: counters[name] for name in buckets.COUNTERS},
        reported_cost=cost,
    )
