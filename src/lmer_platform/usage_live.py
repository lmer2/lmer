"""The live usage view's collector (#315), read-only.

:class:`UsageLive` is a daemon thread. Every tick it reads the transcripts of
every session this host can name, incrementally, and samples each live
session's published limit readings. It never writes to the work repo: the
durable per-session record is the run's own (``work usage``), and what this
module keeps under ``<platform>/usage/`` is a cache that lets a restart resume
without re-counting.

Transcripts come from :func:`lmer_platform.transcripts.locate_sources`, so the
transcript view's containment applies. The invariant the locking rests on: a
published view is built outside the lock, swapped in under it, and never
changed afterwards, so API threads only copy. The rest is in
docs/USAGE-TRACKING.md, "The live view".
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from lmer_usage import buckets
from lmer_usage.incremental import IncrementalReader

from . import registry, runs, statusline, store, transcripts
from .config import PlatformConfig

logger = logging.getLogger(__name__)

STORE_DIRNAME = "usage"
#: Minutes of per-minute burn kept per session; the API's "last 15 minutes" and
#: its fleet windows read from this.
HISTOGRAM_MINUTES = 60
#: Limit readings kept per (harness, operator, window); the projection reads
#: the last ``usage_projection_minutes`` of them.
MAX_SAMPLES = 720
COLLECTED = "collected"
PARTIAL = "partial"
NOT_COLLECTED = "not_collected"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _minute(at: datetime) -> int:
    return int(at.timestamp()) // 60 * 60


def _limits_of(status: dict) -> list:
    """(key, percent, resets_at) windows from one published status document."""
    windows = []
    for window in status.get("limits") or []:
        if isinstance(window, dict) and isinstance(window.get("key"), str):
            windows.append((window["key"], window.get("percent"), window.get("resets_at")))
    account = (status.get("codex") or {}).get("account") or {}
    for window in account.get("limits") or []:
        if isinstance(window, dict) and isinstance(window.get("key"), str):
            windows.append((window["key"], window.get("percent"), window.get("resets_at")))
    return windows


class _Session:
    """The collector's own reader, and the view it last published."""

    __slots__ = ("reader", "revision", "base", "view", "events", "minutes", "run", "settled")

    def __init__(self, reader: Optional[IncrementalReader] = None):
        self.reader = reader or IncrementalReader()
        self.revision = -1
        self.base: Optional[dict] = None
        self.view: Optional[dict] = None
        self.events: tuple = ()
        # minute -> {(harness, model): counters}
        self.minutes: dict = {}
        self.run = None
        self.settled = False


def _summary_view(summary) -> dict:
    return {
        "totals": dict(summary.totals),
        "models": summary.row_list(),
        "not_collected": [dict(entry) for entry in summary.not_collected],
        "started_at": summary.started_at.isoformat() if summary.started_at else None,
        "ended_at": summary.ended_at.isoformat() if summary.ended_at else None,
        "active_seconds": summary.active_seconds,
        "harness_reported_cost": dict(summary.reported_cost),
        "_harness": (sorted(summary.harnesses) or [None])[0],
    }


def _minutes(events, cutoff: int) -> dict:
    out: dict = {}
    for event in events:
        if event.at is None:
            continue
        minute = _minute(event.at)
        if minute >= cutoff:
            rows = out.setdefault(minute, {})
            buckets.add(rows.setdefault((event.harness, event.model), buckets.zero()), event.counters)
    return out


class UsageLive:
    """Reads every known session's usage on a timer; see the module docstring.

    Seams for tests: *sessions* (registry entries), *tracked* (tracked runs),
    *sources* (session id -> transcript paths), *statuses* (entries -> status
    documents), *clock* and *sleep*. :meth:`tick_once` is where every decision
    lives, and it never raises.
    """

    def __init__(self, config: PlatformConfig, *, store_dir: Optional[Path] = None,
                 sessions: Callable = None, tracked: Callable = None, sources: Callable = None,
                 statuses: Callable = None, clock: Callable = None, sleep: Callable = None) -> None:
        self.config = config
        self.interval = config.usage_interval_seconds
        self._store_dir = store_dir
        self._sessions = sessions or (lambda: registry.list_sessions(live_only=False))
        self._tracked = tracked or runs.list_tracked
        self._sources = sources or (lambda sid: [s.path for s in transcripts.locate_sources(sid)])
        self._statuses = statuses or statusline.by_session
        self._clock = clock or _now
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self._lock = threading.Lock()
        self._state: dict = {}
        self._samples: dict = {}
        self._loaded = False
        self.last_tick_at: Optional[datetime] = None
        self.last_error: Optional[str] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="lmer-platform-usage", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            self.tick_once()
            self._sleep(self.interval)

    # -- the store ---------------------------------------------------------

    def store_dir(self) -> Path:
        return self._store_dir or (store.platform_dir() / STORE_DIRNAME)

    def _store_path(self, session_id: str) -> Path:
        return self.store_dir() / f"{transcripts._validated(session_id)}.json"

    def _load(self) -> None:
        self._loaded = True
        directory = self.store_dir()
        if not directory.is_dir():
            return
        for path in sorted(directory.glob("*.json")):
            try:
                data = store.read_json(path)
            except store.StoreError as exc:
                logger.warning("platform_usage_cache_unreadable path=%s error=%s", path.name, exc)
                continue
            if not data:
                continue
            self._state[path.stem] = _Session(IncrementalReader.from_json(data.get("reader")))

    def _save(self, session_id: str, session: _Session) -> None:
        try:
            store.ensure_state_dir(self.store_dir())
            store.write_json(self._store_path(session_id), {"reader": session.reader.to_json()})
        except (OSError, store.StoreError, ValueError) as exc:
            logger.warning("platform_usage_cache_write_failed session=%s error=%s", session_id, exc)

    def _forget(self, gone: list) -> None:
        with self._lock:
            for sid in gone:
                self._state.pop(sid, None)
        for sid in gone:
            try:
                self._store_path(sid).unlink(missing_ok=True)
            except (OSError, ValueError) as exc:
                logger.warning("platform_usage_cache_remove_failed session=%s error=%s", sid, exc)

    # -- one pass ----------------------------------------------------------

    def _known_sessions(self) -> dict:
        """session id -> (registry entry or None, run key or None)."""
        known: dict = {}
        for entry in self._sessions() or []:
            sid = entry.get("id") if isinstance(entry, dict) else None
            if isinstance(sid, str) and sid:
                known[sid] = (entry, runs.run_key_of_entry(entry))
        for run in self._tracked() or []:
            last = getattr(run, "last_session_id", None)
            if isinstance(last, str) and last and last not in known:
                known[last] = (None, runs.key_parts_lenient({
                    "host": run.host, "project": run.project, "slug": run.slug,
                    "uid": getattr(run, "uid", None),
                }))
        return known

    def _read(self, sid: str, entry: Optional[dict], key, now: datetime) -> bool:
        with self._lock:
            session = self._state.setdefault(sid, _Session())
        live = bool((entry or {}).get("live"))
        if session.settled and not live and session.run == key and session.view is not None:
            return False
        task = (entry or {}).get("task") or {}
        try:
            paths = list(self._sources(sid))
        except Exception as exc:  # a malformed id or an unreadable dir is one session's problem
            paths = []
            reason = f"transcripts not located: {exc}"
        else:
            reason = None if paths else "no transcript on this host"
        reader = session.reader
        before, revision = list(reader.not_collected), reader.revision
        if paths:
            reader.update(paths)
        base, events, minutes = session.base, session.events, session.minutes
        if base is None or reader.revision != session.revision or reader.not_collected != before:
            base = _summary_view(reader.summary())
            events = tuple(reader.events())
            minutes = _minutes(events, _minute(now - timedelta(minutes=HISTOGRAM_MINUTES)))
        if reason:
            status = NOT_COLLECTED
        elif base["not_collected"]:
            status = PARTIAL if base["totals"].get("requests") else NOT_COLLECTED
            reason = base["not_collected"][0].get("reason")
        else:
            status = COLLECTED
        view = {name: value for name, value in base.items() if not name.startswith("_")}
        view.update({
            "session": sid, "run": key, "harness": task.get("harness") or base["_harness"],
            "operator": task.get("operator"), "live": live, "status": status, "reason": reason,
            "collected_at": now.isoformat(),
        })
        with self._lock:
            session.base, session.events, session.minutes, session.view = base, events, minutes, view
            session.revision, session.run, session.settled = reader.revision, key, not live
        # Saved when anything the summary shows moved: events or spans.
        return reader.revision != revision

    def _sample(self, entries: list, now: datetime) -> None:
        try:
            statuses = self._statuses(entries) or {}
        except Exception as exc:
            logger.warning("platform_usage_status_read_failed error=%s", exc)
            return
        by_id = {e.get("id"): e for e in entries if isinstance(e, dict)}
        with self._lock:
            for sid, status in statuses.items():
                if not isinstance(status, dict):
                    continue
                task = (by_id.get(sid) or {}).get("task") or {}
                harness = status.get("harness") or task.get("harness")
                operator = task.get("operator")
                for window, percent, resets_at in _limits_of(status):
                    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
                        continue
                    group = self._samples.setdefault((harness, operator, window), deque(maxlen=MAX_SAMPLES))
                    group.append({"at": now, "session": sid, "percent": float(percent),
                                  "resets_at": resets_at, "updated": status.get("updated")})

    def tick_once(self) -> dict:
        """One pass. Returns ``{"sessions": n, "changed": n}``; never raises."""
        now = self._clock()
        try:
            if not self._loaded:
                self._load()
            known = self._known_sessions()
            if known:
                with self._lock:
                    gone = [sid for sid in self._state if sid not in known]
                self._forget(gone)
            changed = 0
            for sid, (entry, key) in known.items():
                try:
                    if self._read(sid, entry, key, now):
                        changed += 1
                        self._save(sid, self._state[sid])
                except Exception as exc:
                    logger.warning("platform_usage_session_failed session=%s error=%s", sid, exc)
            self._sample([e for e, _ in known.values() if e], now)
            self.last_tick_at, self.last_error = now, None
            return {"sessions": len(known), "changed": changed}
        except Exception as exc:
            self.last_error = str(exc)
            logger.warning("platform_usage_tick_failed error=%s", exc)
            return {"sessions": 0, "changed": 0}

    # -- what the API reads -------------------------------------------------

    def session_usage(self, sid: str, *, now: Optional[datetime] = None,
                      burn_minutes: int = 15) -> Optional[dict]:
        """One session's published view, with its burn over ``burn_minutes``:
        ``burn`` in total and ``burn_models`` per (harness, model)."""
        with self._lock:
            session = self._state.get(sid)
            if session is None or session.view is None:
                return None
            view, minutes = session.view, session.minutes
        since = _minute((now or self._clock()) - timedelta(minutes=burn_minutes))
        burn, per_model = buckets.zero(), {}
        for minute, rows in minutes.items():
            if minute >= since:
                for model_key, counters in rows.items():
                    buckets.add(burn, counters)
                    buckets.add(per_model.setdefault(model_key, buckets.zero()), counters)
        return {**view, "totals": dict(view["totals"]), "models": list(view["models"]), "burn": burn,
                "burn_models": [{"harness": h, "model": m, "status": "collected", **c}
                                for (h, m), c in per_model.items()]}

    def session_events(self, sid: str) -> list:
        with self._lock:
            session = self._state.get(sid)
            return list(session.events) if session is not None else []

    def sessions(self) -> list:
        with self._lock:
            return [sid for sid, session in self._state.items() if session.view is not None]

    def sessions_by_run(self) -> dict:
        """run key -> the session ids published for it, for one request to index once."""
        out: dict = {}
        with self._lock:
            for sid, session in self._state.items():
                if session.view is not None and session.run is not None:
                    out.setdefault(session.run, []).append(sid)
        return out

    def samples(self) -> dict:
        """(harness, operator, window) -> readings, oldest first."""
        with self._lock:
            return {key: list(group) for key, group in self._samples.items()}


_CURRENT: Optional[UsageLive] = None


def register(live: Optional[UsageLive]) -> None:
    global _CURRENT
    _CURRENT = live


def current() -> Optional[UsageLive]:
    return _CURRENT
