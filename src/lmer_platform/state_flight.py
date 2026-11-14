"""One ``GET /api/state`` build shared by the requests that can use it (#561).

Every request used to build the fleet view from scratch. The build is CPU-bound,
so N concurrent requests each took about N times one build, and a client that
timed out did not cancel its build — its retry started another. This module lets
requests share a build, within the operator's freshness rule (decision 000008):
**join a build already in flight; never reuse one that has finished.**

A request may join the running build for its project scope only if that build
started at the current write generation and mirror head, and is younger than
:data:`MAX_JOIN_AGE`. Otherwise it joins the scope's *pending* build — one that
has not started yet and so, when it starts, starts after this request arrived —
or creates it. A scope has at most one running and one pending build: the
pending one starts when the running one finishes, so every request that
arrives during a build shares the next one rather than each starting its own.
At most :data:`MAX_BUILDS` builds run at once across all scopes, and nobody
waits longer than :data:`JOIN_DEADLINE`: past it the request is answered 503
with ``Retry-After`` and its thread is released.

The scope is part of the key because a build's slot rows depend on the bound
project (``slots.py``): callers in different projects never share one.
"""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

#: How many state builds may run at once, across every project scope.
MAX_BUILDS = 3
#: A running build older than this is not joined; the request waits for the
#: scope's next build instead.
MAX_JOIN_AGE = 10.0
#: The longest a request waits for someone else's build. Inside the 30 s most
#: clients give up at, so they get a 503 they can act on rather than a timeout.
JOIN_DEADLINE = 25.0

#: The request methods that change state: their response bumps the generation.
_READS = frozenset({"GET", "HEAD", "OPTIONS"})


def _refused(status: int) -> bool:
    """A client error: the request was turned away and applied nothing."""
    return 400 <= status < 500


class FlightTimeout(Exception):
    """No build was available within :data:`JOIN_DEADLINE`."""


@dataclass
class _Flight:
    generation: int
    head: Optional[str]
    started: Optional[float] = None
    claimed: bool = False
    members: int = 0
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: Optional[BaseException] = None


class StateFlights:
    """Shared ``/api/state`` builds, keyed by project scope. Thread-safe."""

    def __init__(self, *, max_builds: Optional[int] = None, join_age: Optional[float] = None,
                 deadline: Optional[float] = None, clock: Callable[[], float] = time.monotonic):
        # Read at construction so a test can set the module constants first.
        self.max_builds = MAX_BUILDS if max_builds is None else max_builds
        self.join_age = MAX_JOIN_AGE if join_age is None else join_age
        self.deadline = JOIN_DEADLINE if deadline is None else deadline
        self._clock = clock
        self._cond = threading.Condition()
        self._running: dict = {}
        self._pending: dict = {}
        self._active = 0
        self._joiners = 0
        self.generation = 0

    def bump(self) -> None:
        """A write completed: no later read may join a build from before it."""
        with self._cond:
            self.generation += 1

    def get(self, scope, head: Optional[str], build: Callable[[], Any]) -> Any:
        """The result of a build this request may use, building it if it must.

        Raises :class:`FlightTimeout` past the deadline, and the build's own
        exception when the build it waited for failed.
        """
        deadline = self._clock() + self.deadline
        with self._cond:
            flight, lead = self._choose(scope, head)
            if flight is not None and not lead and flight.started is None:
                lead = self._await_slot(scope, flight, deadline)
            if not lead:
                self._joiners += 1
        if lead:
            return self._run(scope, flight, build)
        try:
            if not flight.done.wait(max(0.0, deadline - self._clock())):
                raise FlightTimeout(f"no state build finished within {self.deadline:.0f}s")
        finally:
            with self._cond:
                self._joiners -= 1
        if flight.error is not None:
            # Each joiner raises its own copy: raising the one shared object in
            # several threads interleaves their frames into one traceback.
            raise _copied(flight.error) from flight.error
        return flight.result

    def _choose(self, scope, head):
        """Pick (flight, lead) for a new request. Call under the condition."""
        now = self._clock()
        running = self._running.get(scope)
        if (running is not None and running.generation == self.generation and running.head == head
                and now - running.started <= self.join_age):
            return running, False
        pending = self._pending.get(scope)
        if pending is not None:
            pending.members += 1
            return pending, False
        flight = _Flight(self.generation, head)
        if self._can_start(scope):
            self._start(scope, flight)
            return flight, True
        flight.members = 1
        self._pending[scope] = flight
        return flight, False

    def _can_start(self, scope) -> bool:
        """A slot is free and the scope has no build running. Call under the condition."""
        return self._active < self.max_builds and scope not in self._running

    def _start(self, scope, flight) -> None:
        flight.claimed = True
        flight.started = self._clock()
        flight.generation = self.generation
        self._running[scope] = flight
        self._active += 1

    def _await_slot(self, scope, flight, deadline) -> bool:
        """Wait as a member of a pending build; whether this request runs it."""
        while not flight.claimed:
            if self._can_start(scope):
                if self._pending.get(scope) is flight:
                    del self._pending[scope]
                self._start(scope, flight)
                return True
            remaining = deadline - self._clock()
            if remaining <= 0:
                flight.members -= 1
                if flight.members <= 0 and self._pending.get(scope) is flight:
                    del self._pending[scope]
                raise FlightTimeout(f"no state build could start within {self.deadline:.0f}s")
            self._cond.wait(remaining)
        return False

    def _run(self, scope, flight, build):
        try:
            flight.result = build()
            return flight.result
        except BaseException as exc:
            flight.error = exc
            raise
        finally:
            # Under the same lock that hands out the result, so a request that
            # arrives after this point builds fresh rather than joining.
            with self._cond:
                self._active -= 1
                if self._running.get(scope) is flight:
                    del self._running[scope]
                flight.done.set()
                self._cond.notify_all()

    def snapshot(self) -> dict:
        with self._cond:
            return {"running": self._active, "pending": len(self._pending), "joiners": self._joiners}


def _copied(error: BaseException) -> BaseException:
    """A fresh exception of the same type and contents, or *error* itself when
    its type cannot be rebuilt from its own state."""
    try:
        return copy.copy(error)
    except Exception:  # noqa: BLE001 - an uncopyable type keeps the old behaviour
        return error


class WriteGeneration:
    """Pure ASGI middleware: a write's response start bumps the generation.

    At ``http.response.start`` rather than at the end, so the bump happens
    before the client can see the response — a GET it sends afterwards can
    never join a build that started before the write.

    A 4xx response does not bump: a refused request — unauthenticated,
    invalid, or turned away by a conflict — applied nothing, so it must not
    cost the next read its shared build. Without this, any client could keep
    :data:`MAX_BUILDS` builds running with refused POSTs. A 5xx still bumps,
    since a failure part-way may have applied something.
    """

    def __init__(self, app, *, flights: StateFlights):
        self.app = app
        self.flights = flights

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method", "GET") in _READS:
            await self.app(scope, receive, send)
            return

        async def bumping_send(message):
            if message.get("type") == "http.response.start" and not _refused(message.get("status", 200)):
                self.flights.bump()
            await send(message)

        await self.app(scope, receive, bumping_send)
