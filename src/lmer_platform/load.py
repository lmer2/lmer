"""What the daemon is doing right now, for ``GET /api/health`` (issue #561).

``/api/health`` used to report only that the process answers. #561 was a
daemon that answered health in milliseconds while ``/api/state`` took a minute:
the handlers waited on the event loop, on sqlite's write lock and on the
reconcile loop, none of which health touched. This module keeps the numbers
that show that state, measured where it happens:

- :class:`RequestTracker` — requests that have arrived and not yet started a
  response, as pure ASGI middleware, so a request still waiting is visible
  while it waits rather than only once it ends.
- :class:`LoopMonitor` — event-loop lag and threadpool use, sampled by a task
  on the loop itself, which needs no pool thread to report a full pool.
- :data:`BUILDS`, :data:`RECONCILE` and :data:`DB_WRITE_WAIT` — the state
  build's stages, the project supervisor's pass timing, and time spent waiting
  for the projects database's write lock.

Every threshold below is a judgement, not a guarantee. They feed
``degraded``/``degraded_reasons``, which describe a condition and trigger
nothing; ``ok`` still means only that the process answers.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import parse_qs

logger = logging.getLogger(__name__)

#: A request waiting this long past its own allowance is logged when it starts
#: its response (``platform_slow_request``).
SLOW_REQUEST_SECONDS = 5.0
#: A request still waiting this long past its allowance is logged while it waits
#: (``platform_request_stuck``) — the log line a request that never finishes
#: would otherwise never get.
STUCK_REQUEST_SECONDS = 30.0
#: ``degraded`` thresholds. 30 s is the timeout most clients of this daemon use
#: (``curl -m 30`` watches, ``lmer-ctl``), so a read past it has failed its caller.
DEGRADED_READ_SECONDS = 30.0
DEGRADED_LOOP_LAG_SECONDS = 2.0
DEGRADED_RECONCILE_INTERVAL_SECONDS = 30.0
DEGRADED_STATE_BUILD_SECONDS = 30.0
#: The long-poll routes cap the wait a caller may ask for at 30 s.
LONG_POLL_CAP_SECONDS = 30.0
#: Only reads count toward ``degraded``: a spawn, a rotation or a rescan is
#: slow by design, and a flag that fires on every rotation gets ignored.
READ_METHODS = frozenset({"GET", "HEAD"})
LOOP_TICK_SECONDS = 1.0
WINDOW_SECONDS = 60.0
#: Where :class:`RequestTracker` leaves a request's ledger key in its scope state.
IN_FLIGHT_KEY = "in_flight_key"


def _requested_wait(query_string: bytes) -> float:
    """The wait a long-poll asked for, from its own ``timeout`` parameter.

    The caller's declaration, not a guess about the route: only the long-poll
    routes take ``timeout`` (``/api/comms/feed``, ``/api/sessions/{id}/comms``),
    and anything unparseable is no allowance at all.
    """
    try:
        values = parse_qs(query_string.decode("latin-1")).get("timeout")
        requested = float(values[0]) if values else 0.0
    except (ValueError, UnicodeDecodeError):
        return 0.0
    if requested != requested or requested <= 0:  # NaN or nothing asked for
        return 0.0
    return min(requested, LONG_POLL_CAP_SECONDS)


@dataclass
class _Waiting:
    method: str
    path: str
    allowance: float
    started: float
    stuck_logged: bool = False

    def overdue(self, now: float) -> float:
        return max(0.0, now - self.started - self.allowance)

    def describe(self, now: float) -> dict:
        return {"method": self.method, "path": self.path,
                "overdue_seconds": round(self.overdue(now), 3)}


class InFlight:
    """The requests that have arrived and not yet started a response.

    A request is *waiting* from arrival until it sends ``http.response.start``,
    so a streaming response stops counting once it has started answering.
    Written by :class:`RequestTracker` on the event loop and read by
    ``/api/health`` from wherever it runs, hence the lock.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._waiting: dict[int, _Waiting] = {}
        self._ids = itertools.count()

    def arrive(self, method: str, path: str, query_string: bytes) -> tuple[int, _Waiting]:
        entry = _Waiting(method=method, path=path,
                         allowance=_requested_wait(query_string), started=self._clock())
        key = next(self._ids)
        with self._lock:
            self._waiting[key] = entry
        return key, entry

    def finish(self, key: int) -> bool:
        """Stop counting *key*; whether it was still waiting."""
        with self._lock:
            return self._waiting.pop(key, None) is not None

    def log_if_slow(self, entry: _Waiting, scope, status) -> None:
        overdue = entry.overdue(self._clock())
        if overdue < SLOW_REQUEST_SECONDS:
            return
        timings = (scope.get("state") or {}).get("timings")
        logger.warning(
            "platform_slow_request method=%s path=%s status=%s waited=%.1f%s",
            entry.method, entry.path, status, overdue,
            f" stages={timings}" if timings else "",
        )

    def snapshot(self, *, exclude: Optional[int] = None) -> dict:
        """The waiting requests; *exclude* leaves out the caller's own request."""
        now = self._clock()
        with self._lock:
            waiting = [entry for key, entry in self._waiting.items() if key != exclude]
        oldest = max(waiting, key=lambda e: e.overdue(now), default=None)
        reads = [e for e in waiting if e.method in READ_METHODS]
        oldest_read = max(reads, key=lambda e: e.overdue(now), default=None)
        return {
            "waiting": len(waiting),
            "oldest": oldest.describe(now) if oldest else None,
            "oldest_read": oldest_read.describe(now) if oldest_read else None,
        }

    def log_stuck(self) -> None:
        """Log, once each, the requests still waiting past the stuck threshold."""
        now = self._clock()
        with self._lock:
            stuck = [e for e in self._waiting.values()
                     if not e.stuck_logged and e.overdue(now) >= STUCK_REQUEST_SECONDS]
            for entry in stuck:
                entry.stuck_logged = True
        for entry in stuck:
            logger.warning(
                "platform_request_stuck method=%s path=%s waited=%.1f — still "
                "in flight", entry.method, entry.path, entry.overdue(now),
            )


class RequestTracker:
    """Pure ASGI middleware feeding an :class:`InFlight`.

    Only ``http`` scopes are tracked: the tty WebSocket holds its connection by
    design. The query string is read only for a long-poll's ``timeout`` and is
    never kept or logged.
    """

    def __init__(self, app, *, ledger: InFlight):
        self.app = app
        self.ledger = ledger

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        key, entry = self.ledger.arrive(
            scope.get("method", ""), scope.get("path", ""), scope.get("query_string", b""))
        # So /api/health can leave itself out of what it reports.
        scope.setdefault("state", {})[IN_FLIGHT_KEY] = key

        async def tracked_send(message):
            if message.get("type") == "http.response.start" and self.ledger.finish(key):
                self.ledger.log_if_slow(entry, scope, message.get("status"))
            await send(message)

        try:
            await self.app(scope, receive, tracked_send)
        finally:
            self.ledger.finish(key)


class _Window:
    """Timestamped samples kept for :data:`WINDOW_SECONDS`, thread-safe."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._samples: deque = deque()

    def add(self, value: float) -> None:
        now = self._clock()
        with self._lock:
            self._samples.append((now, value))
            self._prune(now)

    def _prune(self, now: float) -> None:
        while self._samples and now - self._samples[0][0] > WINDOW_SECONDS:
            self._samples.popleft()

    def values(self) -> list:
        with self._lock:
            self._prune(self._clock())
            return [value for _, value in self._samples]


class LoopMonitor:
    """Event-loop lag and threadpool use, sampled from a task on the loop.

    The lag is how late ``asyncio.sleep`` wakes: anything that blocks the loop
    thread — a sqlite wait, a registry scan — shows up here whoever caused it.
    The pool numbers come from anyio's default limiter, which is the pool the
    sync handlers run in; reading it needs no pool thread, so a full pool is
    reported rather than hidden.
    """

    def __init__(self, ledger: Optional[InFlight] = None, *,
                 tick: float = LOOP_TICK_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self.ledger = ledger
        self.tick = tick
        self._clock = clock
        self._lags = _Window(clock)
        self._lag: Optional[float] = None
        self._pool: Optional[dict] = None

    def observe_lag(self, lag: float) -> None:
        self._lag = max(0.0, lag)
        self._lags.add(self._lag)

    def sample_pool(self) -> None:
        # The chat listeners import this module (through projects) and never
        # serve HTTP; anyio costs them ~0.6 s cold.
        # deferred: import cost — anyio on the slack/matrix listener entry points
        import anyio.to_thread

        try:
            limiter = anyio.to_thread.current_default_thread_limiter()
            self._pool = {
                "borrowed": limiter.borrowed_tokens,
                "total": limiter.total_tokens,
                "waiting": limiter.statistics().tasks_waiting,
            }
        except Exception as exc:  # noqa: BLE001 - a missing number, not a fault
            logger.debug("platform_pool_sample_failed error=%s", exc)

    async def run(self) -> None:
        while True:
            started = self._clock()
            await asyncio.sleep(self.tick)
            self.observe_lag(self._clock() - started - self.tick)
            self.sample_pool()
            if self.ledger is not None:
                self.ledger.log_stuck()

    def snapshot(self) -> dict:
        lags = self._lags.values()
        return {
            "loop": {
                "lag_seconds": None if self._lag is None else round(self._lag, 3),
                "lag_max_60s": round(max(lags), 3) if lags else None,
            },
            "pool": self._pool,
        }


class Stages:
    """Durations of a build's named stages, each measured from the previous mark."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._start = self._last = clock()
        self.durations: dict[str, float] = {}

    def mark(self, name: str) -> None:
        now = self._clock()
        self.durations[name] = round(now - self._last, 3)
        self._last = now

    @property
    def total(self) -> float:
        return round(self._last - self._start, 3)


@dataclass
class BuildStats:
    """The state build: how many are running, and how the last one went."""

    clock: Callable[[], float] = time.monotonic
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _in_flight: int = 0
    _last: Optional[dict] = None
    _local: threading.local = field(default_factory=threading.local)

    def begin(self) -> Stages:
        with self._lock:
            self._in_flight += 1
        return Stages(self.clock)

    def end(self, stages: Stages, *, finished_at: str) -> dict:
        record = {"seconds": stages.total, "finished_at": finished_at,
                  "stages": dict(stages.durations)}
        with self._lock:
            self._in_flight -= 1
            self._last = record
        self._local.last = record
        return record

    def abandon(self) -> None:
        with self._lock:
            self._in_flight -= 1

    def thread_last(self) -> Optional[dict]:
        """The record of the last build this thread ran, then forgotten."""
        record = getattr(self._local, "last", None)
        self._local.last = None
        return record

    def snapshot(self) -> dict:
        with self._lock:
            return {"in_flight": self._in_flight, "last": self._last}


@dataclass
class ReconcileStats:
    """The project supervisor's passes: duration, the gap between starts, and
    how long the pass now open has been running.

    ``running_seconds`` is what makes a wedged pass visible: the interval only
    updates when the *next* pass starts, so a pass that never finishes would
    otherwise leave the last good interval on show indefinitely.
    """

    clock: Callable[[], float] = time.monotonic
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _previous_start: Optional[float] = None
    _interval: Optional[float] = None
    _open_since: Optional[float] = None
    _last: Optional[dict] = None

    def started(self) -> float:
        now = self.clock()
        with self._lock:
            if self._previous_start is not None:
                self._interval = now - self._previous_start
            self._previous_start = now
            self._open_since = now
        return now

    def finished(self, started: float, *, finished_at: str) -> None:
        with self._lock:
            self._open_since = None
            self._last = {
                "last_seconds": round(self.clock() - started, 3),
                "interval_seconds": None if self._interval is None else round(self._interval, 3),
                "finished_at": finished_at,
            }

    def snapshot(self) -> Optional[dict]:
        with self._lock:
            if self._last is None and self._open_since is None:
                return None
            report = dict(self._last or {})
            report["running_seconds"] = (
                None if self._open_since is None else round(self.clock() - self._open_since, 3))
            return report


class WaitStats:
    """Waits over the last :data:`WINDOW_SECONDS`: count, total and maximum."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._window = _Window(clock)

    def record(self, seconds: float) -> None:
        self._window.add(max(0.0, seconds))

    def snapshot(self) -> dict:
        values = self._window.values()
        return {"count_60s": len(values), "total_60s": round(sum(values), 3),
                "max_60s": round(max(values), 3) if values else 0.0}


BUILDS = BuildStats()
RECONCILE = ReconcileStats()
DB_WRITE_WAIT = WaitStats()


def assess(load: dict) -> list:
    """The reasons the daemon reads as degraded, or ``[]``.

    One sentence per reason, naming the number and the threshold it crossed, so
    a watch can alert on ``degraded`` and a person can read why.
    """
    reasons = []
    read = (load.get("requests") or {}).get("oldest_read")
    if read and read["overdue_seconds"] >= DEGRADED_READ_SECONDS:
        reasons.append(
            f"{read['method']} {read['path']} has waited {read['overdue_seconds']:.0f}s "
            f"(threshold {DEGRADED_READ_SECONDS:.0f}s)"
        )
    lag = ((load.get("loop") or {}).get("lag_max_60s"))
    if lag is not None and lag >= DEGRADED_LOOP_LAG_SECONDS:
        reasons.append(
            f"the event loop lagged {lag:.1f}s in the last minute "
            f"(threshold {DEGRADED_LOOP_LAG_SECONDS:.0f}s)"
        )
    reconcile = load.get("reconcile") or {}
    interval = reconcile.get("interval_seconds")
    running = reconcile.get("running_seconds")
    if running is not None and running >= DEGRADED_RECONCILE_INTERVAL_SECONDS:
        # A pass that has not finished: the interval cannot show it yet.
        reasons.append(
            f"a project reconcile pass has been running for {running:.0f}s "
            f"(threshold {DEGRADED_RECONCILE_INTERVAL_SECONDS:.0f}s)"
        )
    elif interval is not None and interval >= DEGRADED_RECONCILE_INTERVAL_SECONDS:
        reasons.append(
            f"project reconcile passes are {interval:.0f}s apart "
            f"(threshold {DEGRADED_RECONCILE_INTERVAL_SECONDS:.0f}s)"
        )
    build = ((load.get("state_build") or {}).get("last")) or {}
    seconds = build.get("seconds")
    if seconds is not None and seconds >= DEGRADED_STATE_BUILD_SECONDS:
        reasons.append(
            f"the last state build took {seconds:.0f}s "
            f"(threshold {DEGRADED_STATE_BUILD_SECONDS:.0f}s)"
        )
    return reasons
