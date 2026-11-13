"""``GET /api/state`` shares a build in flight, within its bounds (#561).

Each request used to build the fleet view from scratch, so concurrent requests
multiplied the cost and abandoned ones kept building. These tests pin the shared
build's rules: join only a build in flight that started after your last write and
pull, in your own project scope; never reuse a finished one; never wait past the
deadline; never run ``git fetch`` inside a shared build.
"""

import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from lmer_platform import api, load, projects, state_flight, store
from lmer_platform import config as cfg
from lmer_platform import project_context
from lmer_platform.workrepo import MirrorStatus, mirror_status
from tests.conftest import strip_lmer_env

SECRET = "test-secret-value"
AUTH = {"Authorization": f"Bearer {SECRET}"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    strip_lmer_env(monkeypatch)
    monkeypatch.delenv("LMER_WORK_REPO", raising=False)
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    monkeypatch.setattr(load, "BUILDS", load.BuildStats())


@pytest.fixture
def config():
    return cfg.load()


class Gate:
    """A build that waits to be released, counting how often it ran."""

    def __init__(self):
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.scopes = []
        self.lock = threading.Lock()

    def build(self):
        with self.lock:
            self.calls += 1
            number = self.calls
        self.scopes.append(project_context.current())
        self.entered.set()
        assert self.release.wait(20)
        return {"build": number}


def _in_threads(count, target):
    results = [None] * count

    def run(index):
        try:
            results[index] = target()
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            results[index] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    return threads, results


# --- the flight rules --------------------------------------------------------


def test_concurrent_requests_in_one_scope_share_one_build():
    flights, gate = state_flight.StateFlights(), Gate()
    threads, results = _in_threads(5, lambda: flights.get("alpha", "h1", gate.build))
    assert gate.entered.wait(10)
    time.sleep(0.2)
    gate.release.set()
    for thread in threads:
        thread.join(10)
    assert gate.calls == 1
    assert results == [{"build": 1}] * 5


def test_requests_in_different_scopes_never_share_a_build():
    flights, gate = state_flight.StateFlights(), Gate()

    def scoped(workspace_id):
        with project_context.bound(workspace_id):
            return flights.get(workspace_id, "h1", gate.build)

    first, _ = _in_threads(1, lambda: scoped("alpha"))
    assert gate.entered.wait(10)
    second, results = _in_threads(1, lambda: scoped("beta"))
    time.sleep(0.2)
    gate.release.set()
    for thread in first + second:
        thread.join(10)
    assert gate.calls == 2
    assert sorted(gate.scopes) == ["alpha", "beta"]


def test_a_write_or_a_new_head_means_the_next_request_waits_for_a_fresh_build():
    for change in ("write", "head"):
        flights, gate = state_flight.StateFlights(), Gate()
        early, early_results = _in_threads(1, lambda: flights.get("alpha", "h1", gate.build))
        assert gate.entered.wait(10)
        head = "h1"
        if change == "write":
            flights.bump()
        else:
            head = "h2"
        late, late_results = _in_threads(1, lambda: flights.get("alpha", head, gate.build))
        time.sleep(0.2)
        # One build per scope at a time: the fresh build waits for the running one.
        assert gate.calls == 1, change
        gate.release.set()
        for thread in early + late:
            thread.join(10)
        assert gate.calls == 2, change
        assert early_results == [{"build": 1}]
        assert late_results == [{"build": 2}], change


def test_requests_that_arrive_during_a_build_share_the_next_one():
    """At most one running and one pending build per scope (plan T3): requests
    that cannot join the running build coalesce into one pending build, rather
    than each starting a build of its own while a slot is free."""
    flights, gate = state_flight.StateFlights(max_builds=3), Gate()
    early, early_results = _in_threads(1, lambda: flights.get("alpha", "h1", gate.build))
    assert gate.entered.wait(10)
    flights.bump()
    late, late_results = _in_threads(3, lambda: flights.get("alpha", "h1", gate.build))
    time.sleep(0.3)
    assert gate.calls == 1
    assert flights.snapshot() == {"running": 1, "pending": 1, "joiners": 0}
    gate.release.set()
    for thread in early + late:
        thread.join(10)
    assert gate.calls == 2
    assert early_results == [{"build": 1}]
    assert late_results == [{"build": 2}] * 3


def test_a_finished_build_is_never_reused():
    flights, gate = state_flight.StateFlights(), Gate()
    gate.release.set()
    assert flights.get("alpha", "h1", gate.build) == {"build": 1}
    assert flights.get("alpha", "h1", gate.build) == {"build": 2}


def test_a_build_that_never_finishes_holds_nobody_past_the_deadline():
    flights, gate = state_flight.StateFlights(deadline=0.5, max_builds=1), Gate()
    stuck, _ = _in_threads(1, lambda: flights.get("alpha", "h1", gate.build))
    assert gate.entered.wait(10)
    started = time.monotonic()
    with pytest.raises(state_flight.FlightTimeout):
        flights.get("alpha", "h1", gate.build)  # a joiner
    with pytest.raises(state_flight.FlightTimeout):
        flights.get("beta", "h1", gate.build)  # no slot free
    assert time.monotonic() - started < 5
    assert flights.snapshot() == {"running": 1, "pending": 0, "joiners": 0}
    gate.release.set()
    stuck[0].join(10)


def test_a_failed_build_fails_its_joiners_and_the_next_request_builds_fresh():
    flights = state_flight.StateFlights()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def failing():
        calls.append(1)
        entered.set()
        release.wait(10)
        raise RuntimeError("the mirror is unreadable")

    leader, leader_results = _in_threads(1, lambda: flights.get("alpha", "h1", failing))
    assert entered.wait(10)
    joiner, joiner_results = _in_threads(1, lambda: flights.get("alpha", "h1", failing))
    time.sleep(0.2)
    release.set()
    for thread in leader + joiner:
        thread.join(10)
    assert isinstance(leader_results[0], RuntimeError)
    assert isinstance(joiner_results[0], RuntimeError)
    assert calls == [1]
    assert flights.get("alpha", "h1", lambda: "fresh") == "fresh"


def test_each_joiner_raises_its_own_copy_of_a_failed_build():
    """One exception raised in several threads collects all their frames."""
    flights = state_flight.StateFlights()
    entered, release = threading.Event(), threading.Event()

    def failing():
        entered.set()
        release.wait(10)
        raise state_flight.FlightTimeout("the build failed")

    leader, leader_results = _in_threads(1, lambda: flights.get("alpha", "h1", failing))
    assert entered.wait(10)
    joiners, joiner_results = _in_threads(2, lambda: flights.get("alpha", "h1", failing))
    time.sleep(0.2)
    release.set()
    for thread in leader + joiners:
        thread.join(10)
    original = leader_results[0]
    assert all(type(error) is type(original) and error.args == original.args for error in joiner_results)
    assert len({id(error) for error in [original, *joiner_results]}) == 3
    assert all(error.__cause__ is original for error in joiner_results)


def test_a_refused_write_does_not_bump_the_generation():
    """A 4xx applied nothing, so it must not cost the next read its shared
    build; a success or a 5xx (which may have applied part) still bumps."""
    flights = state_flight.StateFlights()

    def app_answering(status):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": status, "headers": []})
            await send({"type": "http.response.body", "body": b""})
        return state_flight.WriteGeneration(app, flights=flights)

    async def post(status):
        async def send(message):
            pass
        await app_answering(status)({"type": "http", "method": "POST"}, None, send)

    for status, bumped in ((401, False), (403, False), (409, False), (200, True), (500, True)):
        before = flights.generation
        asyncio.run(post(status))
        assert (flights.generation > before) is bumped, status


def test_concurrent_builds_never_exceed_the_cap():
    flights = state_flight.StateFlights(max_builds=2)
    running, peak, lock = [0], [0], threading.Lock()
    release = threading.Event()

    def build():
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        release.wait(10)
        with lock:
            running[0] -= 1
        return "ok"

    threads, results = _in_threads(6, lambda: flights.get(threading.current_thread().name, None, build))
    time.sleep(0.5)
    release.set()
    for thread in threads:
        thread.join(15)
    assert peak[0] == 2
    assert results == ["ok"] * 6


# --- through the app ---------------------------------------------------------


def test_a_get_after_a_write_never_joins_the_earlier_build(config):
    gate = Gate()
    app = api.create_app(config, SECRET, state_builder=lambda config, **_k: gate.build())
    with TestClient(app) as client:
        early, early_results = _in_threads(1, lambda: client.get("/api/state", headers=AUTH).json())
        assert gate.entered.wait(10)
        # Any write: its response start bumps the generation before we see it.
        assert client.post("/api/prune", headers=AUTH).status_code == 200
        late, late_results = _in_threads(1, lambda: client.get("/api/state", headers=AUTH).json())
        time.sleep(0.3)
        gate.release.set()
        for thread in early + late:
            thread.join(20)
    assert early_results == [{"build": 1}]
    assert late_results == [{"build": 2}]


def test_a_request_past_the_deadline_gets_a_503_it_can_retry(config, monkeypatch):
    monkeypatch.setattr(state_flight, "JOIN_DEADLINE", 0.5)
    gate = Gate()
    app = api.create_app(config, SECRET, state_builder=lambda config, **_k: gate.build())
    with TestClient(app) as client:
        held, _ = _in_threads(1, lambda: client.get("/api/state", headers=AUTH))
        assert gate.entered.wait(10)
        flights_view = client.get("/api/health", headers=AUTH).json()["load"]["state_build"]["flight"]
        busy = client.get("/api/state", headers=AUTH)  # joins the stuck build, then gives up
        gate.release.set()
        held[0].join(20)
    assert flights_view["running"] == 1
    assert busy.status_code == 503
    assert busy.headers["Retry-After"] == api.STATE_RETRY_AFTER_SECONDS
    assert busy.json()["detail"]["code"] == "state_busy"
    assert busy.json()["detail"]["retryable"] is True


def test_no_git_fetch_runs_inside_a_shared_build(config, monkeypatch):
    pulls = []

    def recording_pull(config, force=False):
        pulls.append(threading.current_thread().name)
        return mirror_status(config)

    monkeypatch.setattr(api, "pull", recording_pull)
    # Handed the status of a pull already made, a build pulls nothing.
    api.build_state(config, mirror=mirror_status(config))
    assert pulls == []
    with TestClient(api.create_app(config, SECRET)) as client:
        assert client.get("/api/state", headers=AUTH).status_code == 200
    assert len(pulls) == 1


def test_a_slow_pull_in_one_request_does_not_hold_another(config, monkeypatch):
    first_entered, release = threading.Event(), threading.Event()
    calls = []

    def pull(config, force=False):
        calls.append(1)
        if len(calls) == 1:
            first_entered.set()
            release.wait(20)  # the request that won the throttle, fetching
        return MirrorStatus(present=False, url=None, last_pull_at=None, last_pull_ok=None,
                            last_error=None, head_sha=None)

    monkeypatch.setattr(api, "pull", pull)
    with TestClient(api.create_app(config, SECRET)) as client:
        slow, _ = _in_threads(1, lambda: client.get("/api/state", headers=AUTH))
        assert first_entered.wait(10)
        other, results = _in_threads(1, lambda: client.get("/api/state", headers=AUTH).status_code)
        other[0].join(10)
        finished = not other[0].is_alive()
        release.set()
        slow[0].join(20)
    assert finished, "a request waited on another request's git fetch"
    assert results == [200]
