"""What ``/api/health`` reports about a busy or wedged daemon (issue #561).

#561 was a daemon whose health answered in milliseconds while ``/api/state``
took a minute. These tests pin the numbers that make that state visible: requests
still waiting, event-loop lag, threadpool use, the state build's stages, the
project supervisor's cadence and the projects database's write-lock wait — and
``degraded``, which reads them and names its reasons.

They assert states and orderings, never wall-clock speed, so a slow runner can
only make a lag or a wait *larger*, never fail an assertion.
"""

import asyncio
import logging
import threading
import time

import pytest
from fastapi.testclient import TestClient

from lmer_platform import api, load, projects, store
from lmer_platform import config as cfg
from lmer_platform.workrepo import mirror_status
from tests.conftest import strip_lmer_env

SECRET = "test-secret-value"
AUTH = {"Authorization": f"Bearer {SECRET}"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    strip_lmer_env(monkeypatch)
    monkeypatch.delenv("LMER_WORK_REPO", raising=False)
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    # The process-wide records start empty for every test.
    monkeypatch.setattr(load, "BUILDS", load.BuildStats())
    monkeypatch.setattr(load, "RECONCILE", load.ReconcileStats())
    monkeypatch.setattr(load, "DB_WRITE_WAIT", load.WaitStats())


@pytest.fixture
def config():
    return cfg.load()


def _payload():
    return {"schema": 1, "runs": [], "attention": [], "counts": {},
            "totals": {"runs": 0, "live": 0, "attention": 0}}


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


# --- the in-flight ledger ----------------------------------------------------


@pytest.mark.parametrize("query, allowance", [
    (b"", 0.0),
    (b"timeout=12.5", 12.5),
    (b"since=4&timeout=25", 25.0),
    (b"timeout=900", load.LONG_POLL_CAP_SECONDS),
    (b"timeout=nan", 0.0),
    (b"timeout=-3", 0.0),
    (b"timeout=soon", 0.0),
])
def test_a_long_poll_is_allowed_the_wait_it_asked_for(query, allowance):
    assert load._requested_wait(query) == allowance


def test_a_request_waits_until_its_response_starts_and_long_polls_are_allowed_their_timeout():
    clock = Clock()
    ledger = load.InFlight(clock=clock)
    state_key, _ = ledger.arrive("GET", "/api/state", b"")
    ledger.arrive("GET", "/api/comms/feed", b"timeout=25")
    post_key, _ = ledger.arrive("POST", "/api/assistant/rotate", b"")
    clock.now += 40

    snapshot = ledger.snapshot()
    assert snapshot["waiting"] == 3
    # The long-poll asked for 25 s of its 40, so it is 15 s overdue, not 40.
    assert snapshot["oldest"]["overdue_seconds"] == 40.0
    assert snapshot["oldest_read"] == {"method": "GET", "path": "/api/state", "overdue_seconds": 40.0}

    assert ledger.finish(state_key) is True
    assert ledger.finish(state_key) is False
    snapshot = ledger.snapshot()
    assert snapshot["oldest_read"] == {"method": "GET", "path": "/api/comms/feed", "overdue_seconds": 15.0}
    ledger.finish(post_key)
    assert ledger.snapshot()["waiting"] == 1


def test_a_request_that_never_answers_is_logged_once_while_it_waits(caplog):
    clock = Clock()
    ledger = load.InFlight(clock=clock)
    ledger.arrive("GET", "/api/state", b"")
    clock.now += load.STUCK_REQUEST_SECONDS - 1
    with caplog.at_level(logging.WARNING, logger="lmer_platform.load"):
        ledger.log_stuck()
        assert "platform_request_stuck" not in caplog.text
        clock.now += 2
        ledger.log_stuck()
        ledger.log_stuck()
    assert caplog.text.count("platform_request_stuck method=GET path=/api/state") == 1


def _run_tracked(ledger, *, status=200, release=None, timings=None):
    """Drive one request through RequestTracker around a tiny ASGI app."""
    seen = {}

    async def app(scope, receive, send):
        seen["waiting"] = ledger.snapshot()["waiting"]
        if timings is not None:
            scope.setdefault("state", {})["timings"] = timings
        if release is not None:
            release()
        await send({"type": "http.response.start", "status": status, "headers": []})
        seen["after_start"] = ledger.snapshot()["waiting"]
        await send({"type": "http.response.body", "body": b"{}"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    tracker = load.RequestTracker(app, ledger=ledger)
    scope = {"type": "http", "method": "GET", "path": "/api/state", "query_string": b"secret=x"}
    asyncio.run(tracker(scope, receive, send))
    return seen


def test_the_tracker_counts_a_request_until_its_response_starts():
    ledger = load.InFlight()
    seen = _run_tracked(ledger)
    assert seen == {"waiting": 1, "after_start": 0}
    assert ledger.snapshot()["waiting"] == 0


def test_a_slow_request_is_logged_with_its_build_stages_and_never_its_query(caplog, monkeypatch):
    clock = Clock()
    ledger = load.InFlight(clock=clock)

    def advance():
        clock.now += load.SLOW_REQUEST_SECONDS + 1

    with caplog.at_level(logging.WARNING, logger="lmer_platform.load"):
        _run_tracked(ledger, status=200, release=advance, timings={"pull": 0.1, "inventory": 5.9})
    assert "platform_slow_request method=GET path=/api/state status=200 waited=6.0" in caplog.text
    assert "inventory" in caplog.text
    assert "secret=x" not in caplog.text


def test_websockets_are_not_tracked():
    ledger = load.InFlight()
    calls = []

    async def app(scope, receive, send):
        calls.append(ledger.snapshot()["waiting"])

    asyncio.run(load.RequestTracker(app, ledger=ledger)({"type": "websocket", "path": "/tty"}, None, None))
    assert calls == [0]


# --- degraded ----------------------------------------------------------------


def test_degraded_names_each_threshold_it_crossed_and_only_reads_count():
    quiet = {"requests": {"waiting": 1, "oldest": {"method": "POST", "path": "/api/assistant/rotate",
                                                   "overdue_seconds": 300.0},
                          "oldest_read": None},
             "loop": {"lag_seconds": 0.0, "lag_max_60s": 0.1}}
    assert load.assess(quiet) == []

    busy = {
        "requests": {"oldest_read": {"method": "GET", "path": "/api/state", "overdue_seconds": 45.0}},
        "loop": {"lag_max_60s": 3.1},
        "reconcile": {"interval_seconds": 31.0},
        "state_build": {"last": {"seconds": 42.0}},
    }
    reasons = load.assess(busy)
    assert len(reasons) == 4
    assert "GET /api/state has waited 45s" in reasons[0]
    assert "event loop lagged 3.1s" in reasons[1]
    assert "reconcile passes are 31s apart" in reasons[2]
    assert "state build took 42s" in reasons[3]


# --- the loop monitor --------------------------------------------------------


def test_the_monitor_sees_a_blocked_loop_and_samples_the_pool():
    monitor = load.LoopMonitor(tick=0.05)

    async def scenario():
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.1)
        time.sleep(0.3)  # blocks the loop, as a sqlite wait inside bind() did
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    snapshot = monitor.snapshot()
    assert snapshot["loop"]["lag_max_60s"] >= 0.25
    assert snapshot["pool"]["total"] >= 1
    assert snapshot["pool"]["borrowed"] >= 0


# --- the process-wide records ------------------------------------------------


def test_build_stats_keep_the_last_build_and_hand_it_to_the_thread_that_ran_it():
    clock = Clock()
    stats = load.BuildStats(clock=clock)
    stages = stats.begin()
    assert stats.snapshot()["in_flight"] == 1
    clock.now += 2
    stages.mark("pull")
    clock.now += 3
    stages.mark("inventory")
    record = stats.end(stages, finished_at="2026-11-11T00:00:00Z")
    assert record == {"seconds": 5.0, "finished_at": "2026-11-11T00:00:00Z",
                      "stages": {"pull": 2.0, "inventory": 3.0}}
    assert stats.snapshot() == {"in_flight": 0, "last": record}
    assert stats.thread_last() == record
    assert stats.thread_last() is None

    other = []
    stats.end(stats.begin(), finished_at="x")
    threading.Thread(target=lambda: other.append(stats.thread_last())).start()
    time.sleep(0.05)
    assert other == [None]

    stats.begin()
    stats.abandon()
    assert stats.snapshot()["in_flight"] == 0


def test_reconcile_stats_report_the_gap_between_pass_starts():
    clock = Clock()
    stats = load.ReconcileStats(clock=clock)
    first = stats.started()
    clock.now += 0.5
    stats.finished(first, finished_at="a")
    assert stats.snapshot() == {"last_seconds": 0.5, "interval_seconds": None, "finished_at": "a",
                                "running_seconds": None}
    clock.now += 29.5
    second = stats.started()
    clock.now += 1.0
    stats.finished(second, finished_at="b")
    assert stats.snapshot() == {"last_seconds": 1.0, "interval_seconds": 30.0, "finished_at": "b",
                                "running_seconds": None}


def test_a_reconcile_pass_that_never_finishes_makes_the_daemon_degraded():
    """The interval updates only when the next pass starts, so a wedged pass
    would keep showing the last good one; ``running_seconds`` shows it (#561)."""
    clock = Clock()
    stats = load.ReconcileStats(clock=clock)
    first = stats.started()
    clock.now += 0.5
    stats.finished(first, finished_at="a")
    clock.now += 1.5
    stats.started()  # wedges: never finishes
    clock.now += 5
    assert stats.snapshot()["running_seconds"] == 5.0
    assert load.assess({"reconcile": stats.snapshot()}) == []
    clock.now += load.DEGRADED_RECONCILE_INTERVAL_SECONDS
    snapshot = stats.snapshot()
    assert snapshot["interval_seconds"] is None and snapshot["last_seconds"] == 0.5
    assert load.assess({"reconcile": snapshot}) == [
        f"a project reconcile pass has been running for {5 + load.DEGRADED_RECONCILE_INTERVAL_SECONDS:.0f}s "
        f"(threshold {load.DEGRADED_RECONCILE_INTERVAL_SECONDS:.0f}s)"]


def test_a_first_reconcile_pass_that_never_finishes_is_still_reported():
    clock = Clock()
    stats = load.ReconcileStats(clock=clock)
    stats.started()
    clock.now += 2
    assert stats.snapshot() == {"running_seconds": 2.0}


def test_waits_are_kept_for_a_minute():
    clock = Clock()
    stats = load.WaitStats(clock=clock)
    stats.record(0.25)
    stats.record(1.5)
    assert stats.snapshot() == {"count_60s": 2, "total_60s": 1.75, "max_60s": 1.5}
    clock.now += load.WINDOW_SECONDS + 1
    assert stats.snapshot() == {"count_60s": 0, "total_60s": 0.0, "max_60s": 0.0}


def test_the_supervisor_records_its_own_passes(config, monkeypatch):
    supervisor = projects.Supervisor(config)
    passes = []

    def one_pass(_config):
        passes.append(1)
        if len(passes) == 2:
            supervisor._stop.set()

    monkeypatch.setattr(projects, "reconcile", one_pass)
    monkeypatch.setattr(supervisor._stop, "wait", lambda _seconds: None)
    supervisor.run()
    snapshot = load.RECONCILE.snapshot()
    assert passes == [1, 1]
    assert snapshot["interval_seconds"] is not None
    assert snapshot["last_seconds"] >= 0


def test_the_projects_database_records_its_write_lock_wait():
    with projects.database() as db:
        db.execute("SELECT 1")
    assert load.DB_WRITE_WAIT.snapshot()["count_60s"] == 1


def test_build_state_times_each_stage(config, monkeypatch):
    monkeypatch.setattr(api, "pull", lambda config, force=False: mirror_status(config))
    api.build_state(config)
    last = load.BUILDS.snapshot()["last"]
    assert set(last["stages"]) == {"pull", "reconcile", "publication_wait", "sessions", "inventory", "folds"}
    assert load.BUILDS.snapshot()["in_flight"] == 0


# --- through the app ---------------------------------------------------------


def _held_builder():
    entered, release = threading.Event(), threading.Event()

    def builder(config, *, force_pull=False):
        entered.set()
        assert release.wait(10)
        return _payload()

    return builder, entered, release


def test_health_reports_a_held_read_as_waiting_and_degraded(config, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="lmer_platform.load")
    monkeypatch.setattr(load, "DEGRADED_READ_SECONDS", 0.0)
    monkeypatch.setattr(load, "SLOW_REQUEST_SECONDS", 0.0)
    builder, entered, release = _held_builder()
    with TestClient(api.create_app(config, SECRET, state_builder=builder)) as client:
        held = threading.Thread(target=lambda: client.get("/api/state", headers=AUTH))
        held.start()
        assert entered.wait(10)
        health = client.get("/api/health", headers=AUTH).json()
        release.set()
        held.join(10)
    assert health["ok"] is True
    requests = health["load"]["requests"]
    # The held read, and not the health call answering this.
    assert requests["waiting"] == 1
    assert requests["oldest_read"]["path"] == "/api/state"
    assert health["degraded"] is True
    assert any("GET /api/state has waited" in reason for reason in health["degraded_reasons"])
    assert "platform_slow_request method=GET path=/api/state" in caplog.text


def test_a_held_write_is_reported_but_does_not_make_the_daemon_degraded(config, monkeypatch):
    monkeypatch.setattr(load, "DEGRADED_READ_SECONDS", 0.0)
    builder, entered, release = _held_builder()
    with TestClient(api.create_app(config, SECRET, state_builder=builder)) as client:
        held = threading.Thread(target=lambda: client.post("/api/rescan", headers=AUTH))
        held.start()
        assert entered.wait(10)
        health = client.get("/api/health", headers=AUTH).json()
        release.set()
        held.join(10)
    requests = health["load"]["requests"]
    assert requests["waiting"] == 1
    assert requests["oldest"]["path"] == "/api/rescan"
    assert requests["oldest_read"] is None
    assert health["degraded"] is False


def test_health_carries_the_load_block_at_rest(config):
    with TestClient(api.create_app(config, SECRET, state_builder=lambda *_a, **_k: _payload())) as client:
        body = client.get("/api/health", headers=AUTH).json()
        deadline = time.monotonic() + 10
        while body["load"]["pool"] is None and time.monotonic() < deadline:
            time.sleep(0.2)
            body = client.get("/api/health", headers=AUTH).json()
    assert set(body["load"]) == {"requests", "loop", "pool", "state_build", "reconcile", "db_write_wait"}
    assert body["load"]["pool"]["total"] >= 1
    assert body["load"]["loop"]["lag_seconds"] is not None
    assert body["degraded"] is False
    assert body["degraded_reasons"] == []
    assert body["ok"] is True
