"""Time tracking takes a new configuration without a daemon restart (issue #668).

A settings write hands the freshly loaded config to
:meth:`lmer_platform.timetrack_live.TimeTracking.apply`. With tracking on before
and after, the running parts keep their state and take the new registry, switch
and writer identity. Turning it on or off starts or stops them under the hub,
which keeps the same consumer throughout.
"""
import threading
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from lmer_platform import api, store, timesheet_writer, timetrack, timetrack_activity, timetrack_live
from lmer_platform import config as cfg
from tests.conftest import strip_lmer_env

ALICE = {"key": "alice", "name": "Alice", "slack": ["U0ALICE"]}
BOB = {"key": "bob", "name": "Bob", "slack": ["U0BOB"]}
TIME_THREADS = {"lmer-timesheet-writer", "lmer-time-activity"}


@pytest.fixture(autouse=True)
def clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    yield
    tracking = timetrack_live.get_tracking()
    if tracking is not None:
        tracking.stop()
    timetrack_live.register_tracking(None)
    timetrack.register_tracker(None)
    timesheet_writer.register_writer(None)
    timetrack_activity.register_activity(None)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture
def tracking():
    unit = timetrack_live.TimeTracking()
    timetrack_live.register_tracking(unit)
    return unit


def _time_threads():
    return {thread.name for thread in threading.enumerate()} & TIME_THREADS


def _loaded(**stored):
    store.write_json(cfg.config_path(), stored)
    return cfg.load()


def test_listing_a_person_starts_tracking_in_the_running_daemon(platform_root, tracking):
    tracking.start(_loaded())
    assert not tracking.running and _time_threads() == set()

    outcome = tracking.apply(_loaded(people=[ALICE]))

    assert outcome == timetrack_live.STARTED
    assert timetrack.get_tracker() is tracking.tracker is not None
    assert timetrack_activity.get_activity() is tracking.activity is not None
    assert _time_threads() == TIME_THREADS


def test_a_registry_edit_is_applied_in_place(platform_root, tracking):
    tracking.start(_loaded(people=[ALICE]))
    tracker, activity, writer = tracking.tracker, tracking.activity, tracking.writer
    assert activity.registry.person_for_sender("slack", "U0BOB") is None

    outcome = tracking.apply(_loaded(people=[ALICE, BOB], default_person="bob",
                                     time_writer_name="Time Bot"))

    assert outcome == timetrack_live.UPDATED
    assert (tracking.tracker, tracking.activity, tracking.writer) == (tracker, activity, writer), \
        "an edit must not restart the parts: the activity consumer's dedupe and unmapped list live in memory"
    assert activity.registry.person_for_sender("slack", "U0BOB") == "bob"
    assert tracker.config.default_person == "bob"
    assert writer.config.time_writer_name == "Time Bot"
    assert timetrack_live.current_config(cfg.load()).default_person == "bob"


def test_the_switch_off_stops_tracking_and_keeps_the_registry(platform_root, tracking, fleet_secret):
    tracking.start(_loaded(people=[ALICE]))

    outcome = tracking.apply(_loaded(people=[ALICE], time_tracking=False))

    assert outcome == timetrack_live.STOPPED
    assert timetrack.get_tracker() is None and _time_threads() == set()
    assert cfg.load().people == [ALICE]
    with fleet_secret() as http:
        response = http.get("/api/time/status")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "time_tracking_off"
    assert "switched off" in response.json()["detail"]["message"]


def test_a_problem_stops_tracking_and_the_503_names_it(platform_root, tracking, fleet_secret):
    tracking.start(_loaded(people=[ALICE]))

    outcome = tracking.apply(_loaded(people=[ALICE], time_pause_hours=-1))

    assert outcome == timetrack_live.STOPPED and not tracking.running
    with fleet_secret() as http:
        detail = http.get("/api/time/status").json()["detail"]
    assert any("time_pause_hours" in problem for problem in detail["problems"])


def test_applying_off_to_stopped_tracking_says_off(platform_root, tracking):
    tracking.start(_loaded())
    assert tracking.apply(_loaded(time_tracking=False)) == timetrack_live.OFF


def test_the_hub_consumer_forwards_only_while_there_is_an_activity_consumer(tracking):
    seen = []
    tracking(object())
    tracking.activity = seen.append
    marker = object()
    tracking(marker)
    assert seen == [marker]


@pytest.mark.parametrize("value, runs, problem", [
    (True, True, False), (False, False, False), ("yes", False, True), (1, False, True),
])
def test_the_switch_is_a_boolean(platform_root, value, runs, problem):
    config = _loaded(people=[ALICE], time_tracking=value)
    assert config.time_tracking_runs is runs
    assert any("time_tracking" in item for item in config.time_problems) is problem


@pytest.fixture
def fleet_secret(platform_root):
    def client():
        platform = cfg.load()
        secret = cfg.ensure_secret(platform)
        app = api.create_app(replace(platform), secret, state_builder=lambda *_a, **_k: {"runs": []})
        return TestClient(app, headers={"Authorization": f"Bearer {secret}"})
    return client
