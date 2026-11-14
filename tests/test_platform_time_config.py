"""Time tracking with nobody to track is off, not idle (issue #604).

``people: []`` is the default for every existing install, so it must leave
tracking fully off: no problem to warn about, nothing started, no
``timesheet.sqlite3`` created, and every time route answering ``503
time_tracking_off``. A listed person without ``default_person`` keeps tracking
on; only the operator credential is refused, with ``409
time_no_default_person``.
"""
import logging
import threading
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from lmer_platform import (api, daemon, store, timesheet_store, timesheet_writer, timetrack, timetrack_activity,
                           timetrack_live)
from lmer_platform import config as cfg
from tests.conftest import strip_lmer_env
from tests.test_platform_projects import fleet  # noqa: F401

ALICE = {"key": "alice", "name": "Alice", "slack": ["U0ALICE"]}
BOB = {"key": "bob", "name": "Bob", "slack": ["U0BOB"]}
TIME_THREADS = {"lmer-timesheet-writer", "lmer-time-activity"}


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    yield
    timetrack.register_tracker(None)
    timesheet_writer.register_writer(None)
    timetrack_activity.register_activity(None)
    timetrack_live.register_tracking(None)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


def _time_threads():
    return {thread.name for thread in threading.enumerate()} & TIME_THREADS


def test_no_people_is_off_without_a_problem(platform_root, caplog):
    with caplog.at_level(logging.WARNING, logger="lmer_platform.config"):
        config = cfg.load()

    assert config.people == []
    assert not config.time_tracking_runs
    assert config.time_tracking_enabled and config.time_problems == (), \
        "nobody listed is not a configuration error"
    assert "time tracking is disabled" not in caplog.text


def test_a_listed_person_without_a_default_person_keeps_tracking_on(platform_root):
    store.write_json(cfg.config_path(), {"people": [ALICE]})
    assert cfg.load().time_tracking_runs


def test_with_no_people_the_daemon_starts_no_time_tracking(platform_root, caplog, request):
    with caplog.at_level(logging.INFO, logger="lmer_platform.timetrack_live"):
        tracking = daemon._track_time(cfg.load())
    request.addfinalizer(tracking.stop)

    assert (tracking.tracker, tracking.writer, tracking.activity) == (None, None, None)
    assert not tracking.running
    assert timetrack.get_tracker() is None
    assert timesheet_writer.get_writer() is None
    assert timetrack_activity.get_activity() is None
    assert _time_threads() == set()
    assert not (platform_root / timesheet_store.DB_FILE).exists()
    assert "platform_time_tracking_off" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


def test_with_a_person_listed_the_daemon_starts_time_tracking(platform_root):
    store.write_json(cfg.config_path(), {"people": [ALICE]})
    tracking = daemon._track_time(cfg.load())
    try:
        assert timetrack.get_tracker() is tracking.tracker is not None
        assert timesheet_writer.get_writer() is tracking.writer is not None
        assert tracking.running and tracking.activity is not None
        assert timetrack_live.get_tracking() is tracking
        assert _time_threads() == TIME_THREADS
    finally:
        tracking.stop()
    assert _time_threads() == set()


def _client(platform):
    secret = cfg.ensure_secret(platform)
    app = api.create_app(platform, secret, state_builder=lambda *_a, **_k: {"runs": []})
    return TestClient(app, headers={"Authorization": f"Bearer {secret}"})


def test_with_no_people_the_status_route_is_a_503(fleet, request):
    assert not fleet.time_tracking_runs
    request.addfinalizer(daemon._track_time(fleet).stop)

    with _client(fleet) as http:
        response = http.get("/api/time/status")

    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "time_tracking_off"
    assert detail["problems"] == []
    assert not (store.platform_dir() / timesheet_store.DB_FILE).exists()


def test_people_without_a_default_person_keep_the_409_hint(fleet):
    platform = replace(fleet, people=[dict(ALICE), dict(BOB)])
    assert platform.time_tracking_runs
    timetrack.register_tracker(timetrack.Tracker(platform))

    with _client(platform) as http:
        response = http.get("/api/time/status")

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "time_no_default_person"
