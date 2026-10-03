"""Accepted end requests, control loss, daemon configuration, and fleet receipts."""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import requests
from fastapi.testclient import TestClient

from lmer_cli import comms as bus
from lmer_cli import supervisor
from lmer_cli.container import clone_and_exec
from lmer_platform import comms, config, detect, inventory, registry, session_io, store, teardown
from lmer_platform import spawn
from tests.conftest import strip_lmer_env


@pytest.fixture(autouse=True)
def isolated_platform(tmp_path, monkeypatch):
    strip_lmer_env(monkeypatch)
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    store.write_json(config.config_path(), {"nudge_after_seconds": 0, "checkin_window_seconds": 0})


@pytest.fixture
def clock(monkeypatch):
    value = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return value["now"]

    monkeypatch.setattr(teardown, "datetime", Clock)
    return value


@pytest.fixture
def ending():
    session_id = registry.new_session_id()
    registry.register(session_id, pid=os.getpid())
    with patch.dict(os.environ):
        clone_and_exec.mint_session_id()
        work_session = os.environ["LMER_SESSION_ID"]
    log = bus.MessageLog(work_session, clock=lambda: "2026-01-01T00:00:00Z")
    message, _ = log.append(
        kind=bus.KIND_NOTE, text="Session shutdown requested.",
        source=bus.Source(bus.CHANNEL_SUPERVISOR),
        meta={"event": bus.EVENT_SESSION_END_REQUESTED},
    )
    hub = comms.CommsHub()
    assert hub._ingest(session_id, message.to_dict())
    return session_id, message.to_dict(), hub


def test_default_env_file_and_explicit_grace_precedence(monkeypatch):
    assert config.load().stuck_teardown_seconds == 60
    store.write_json(config.config_path(), {"stuck_teardown_seconds": 120})
    assert config.load().stuck_teardown_seconds == 120
    monkeypatch.setenv("LMER_PLATFORM_STUCK_TEARDOWN_SECONDS", "17")
    assert config.load().stuck_teardown_seconds == 17
    assert config.load({"stuck_teardown_seconds": 9}).stuck_teardown_seconds == 9


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "soon"])
def test_bad_stored_grace_is_refused(value):
    store.write_json(config.config_path(), {"stuck_teardown_seconds": value})
    with pytest.raises(config.ConfigError, match="stuck_teardown_seconds"):
        config.load()


def test_bad_environment_grace_is_refused(monkeypatch):
    monkeypatch.setenv("LMER_PLATFORM_STUCK_TEARDOWN_SECONDS", "soon")
    with pytest.raises(config.ConfigError, match="LMER_PLATFORM_STUCK_TEARDOWN_SECONDS"):
        config.load()


def test_ingested_ack_is_durable_and_replay_keeps_loss_interval(ending):
    session_id, message, hub = ending
    assert session_id.startswith("s-")
    assert isinstance(message["session"], str) and message["session"]
    assert message["session"] != session_id
    events, _ = hub.journal.read_since(0)
    assert events[0].message == message
    assert registry.read_session(session_id)["teardown_requested_at"] == message["at"]
    interval = {"unreachable_since": "2026-01-01T00:00:10Z"}
    registry.update(session_id, teardown=interval)
    assert hub._ingest(session_id, message)
    assert registry.read_session(session_id)["teardown"] == interval


def test_first_fleet_read_failure_does_not_lose_new_teardown(ending, clock, monkeypatch):
    session_id, message, _ = ending
    registry.update(session_id, teardown_requested_at=message["at"],
                    teardown={"unreachable_since": "2026-01-01T00:00:00Z"})
    clock["now"] += timedelta(seconds=60)
    monkeypatch.setattr(teardown, "_reachable", lambda sid: False)
    notes = []
    reads = []

    def state_reader(_config):
        reads.append(True)
        if len(reads) == 1:
            raise store.StoreError("first fleet read failed")
        return inventory.build_inventory([], registry.list_sessions(), activity={}).to_dict()

    detector = detect.Detector(config.load(), state_reader=state_reader,
                               notifier=lambda note, **kwargs: notes.append(note))
    assert detector.tick_once() == []
    assert registry.read_session(session_id)["teardown"]["stuck_at"]
    assert [item.kind for item in detector.tick_once()] == ["stuck_teardown"]
    assert len(notes) == 1 and session_id in notes[0]
    assert detector.tick_once() == []
    assert len(notes) == 1


def test_child_written_receipt_cannot_arm_a_wind_down(clock, monkeypatch):
    session_id = registry.new_session_id()
    registry.register(session_id, pid=os.getpid())
    registry.update(session_id, lifecycle={"verb": "wind_down"})
    directory = spawn._prepare_container_log_dir(session_id)
    assert directory is not None
    assert str(directory) in spawn._container_log_mount_flags(directory)[1]
    forged = {"kind": "note", "session": "child-work-session",
              "source": {"channel": "supervisor"},
              "meta": {"event": bus.EVENT_SESSION_END_REQUESTED},
              "at": "2026-01-01T00:00:00Z"}
    subprocess.run([sys.executable, "-c",
                    "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])",
                    str(directory / "end-request.json"), json.dumps(forged)], check=True)
    monkeypatch.setattr(teardown, "_reachable", lambda sid: False)
    notes = []
    detector = detect.Detector(
        config.load(),
        state_reader=lambda _: inventory.build_inventory([], registry.list_sessions(), activity={}).to_dict(),
        notifier=lambda note, **kwargs: notes.append(note),
    )
    assert detector.tick_once() == []
    assert not registry.read_session(session_id).get("teardown_requested_at")
    clock["now"] += timedelta(seconds=60)
    assert detector.tick_once() == []
    assert notes == []


def test_directory_at_receipt_path_never_leaks_descriptors():
    session_id = registry.new_session_id()
    registry.register(session_id, pid=os.getpid())
    directory = spawn._prepare_container_log_dir(session_id)
    receipt = directory / "end-request.json"
    receipt.mkdir()
    before = len(os.listdir("/proc/self/fd"))
    try:
        for _ in range(3):
            assert teardown.sweep(60) == []
        assert len(os.listdir("/proc/self/fd")) == before
    finally:
        # Close descriptors leaked by the pre-fix regression run itself.
        for name in os.listdir("/proc/self/fd"):
            try:
                if os.readlink(f"/proc/self/fd/{name}") == str(receipt):
                    os.close(int(name))
            except OSError:
                pass


def test_registry_write_failure_leaves_ack_available_for_retry(ending, monkeypatch):
    session_id, message, _ = ending
    message = {**message, "id": "ending-session/000002", "seq": 2}
    hub = comms.CommsHub()
    original = registry.update

    def fail(*args, **kwargs):
        raise store.StoreError("test write failure")

    monkeypatch.setattr(registry, "update", fail)
    assert hub._ingest(session_id, message) is False
    monkeypatch.setattr(registry, "update", original)
    assert hub._ingest(session_id, message) is True


def test_prose_and_wind_down_alone_never_start_detection(monkeypatch):
    registry.register("working", pid=os.getpid())
    registry.update("working", lifecycle={"verb": "wind_down", "requested_at": "2026-01-01T00:00:00Z"})
    teardown.record_request("working", {
        "kind": "note", "text": "Session shutdown requested.",
        "session": "working", "source": {"channel": "agent"},
        "at": "2026-01-01T00:00:00Z", "meta": {"event": bus.EVENT_SESSION_END_REQUESTED},
    })
    sampled = []
    monkeypatch.setattr(teardown, "_reachable", lambda sid: sampled.append(sid))
    assert teardown.sweep(60) == []
    assert sampled == []


def test_agent_cannot_impersonate_the_supervisor_acknowledgment():
    log = bus.MessageLog("test")
    app = supervisor._build_fastapi_app(
        supervisor.OutputBuffer(), lambda data: len(data), "external-test-key",
        agent_token="agent-test-key", comms=log,
    )
    response = TestClient(app).post(
        "/messages", headers={"Authorization": "Bearer agent-test-key"},
        json={"kind": "note", "text": "ending", "source": {"channel": "supervisor"},
              "meta": {"event": bus.EVENT_SESSION_END_REQUESTED}},
    )
    assert response.status_code == 403
    assert log.read_since(0)[0] == []


def test_threshold_recovery_and_second_outage(ending, clock, monkeypatch):
    session_id, _, _ = ending
    reachable = {"value": False}
    monkeypatch.setattr(teardown, "_reachable", lambda sid: reachable["value"])
    assert teardown.sweep(60) == []
    clock["now"] += timedelta(seconds=59)
    assert teardown.sweep(60) == []
    clock["now"] += timedelta(seconds=1)
    assert teardown.sweep(60) == [session_id]
    assert teardown.sweep(60) == []
    reachable["value"] = True
    assert teardown.sweep(60) == []
    assert registry.read_session(session_id)["teardown"] == {}
    reachable["value"] = False
    assert teardown.sweep(60) == []
    clock["now"] += timedelta(seconds=60)
    assert teardown.sweep(60) == [session_id]


def test_dead_launcher_is_not_marked_stuck(ending, monkeypatch):
    session_id, _, _ = ending
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: False)
    sampled = []
    monkeypatch.setattr(teardown, "_reachable", lambda sid: sampled.append(sid))
    assert teardown.sweep(1) == []
    assert sampled == []
    assert not registry.read_session(session_id).get("teardown")


def test_missing_probe_credentials_clear_the_failure_interval(ending, clock, monkeypatch):
    session_id, _, _ = ending
    registry.update(session_id, teardown={"unreachable_since": "2026-01-01T00:00:00Z"})
    clock["now"] += timedelta(seconds=60)

    def unavailable(_session_id):
        raise session_io.ControlUnavailable("credentials are unavailable")

    monkeypatch.setattr(session_io, "control_endpoint", unavailable)
    assert teardown.sweep(60) == []
    assert registry.read_session(session_id)["teardown"] == {}


def test_http_error_is_an_answer_but_connection_failure_is_not(monkeypatch):
    endpoint = session_io.ControlEndpoint(host="127.0.0.1", port=1, token="test-key")
    monkeypatch.setattr(session_io, "control_endpoint", lambda sid: endpoint)
    response = requests.Response()
    response.status_code = 401
    response._content = b'{}'
    calls = []

    def request(*args, **kwargs):
        calls.append(kwargs["timeout"])
        return response

    monkeypatch.setattr(session_io.requests, "request", request)
    assert teardown._reachable("test") is True
    assert calls == [session_io.ACTIVITY_TIMEOUT_SECONDS]

    def refused(*args, **kwargs):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(session_io.requests, "request", refused)
    assert teardown._reachable("test") is False


def test_daemon_honors_env_grace_and_spools_one_named_digest(ending, clock, monkeypatch):
    session_id, _, _ = ending
    monkeypatch.setenv("LMER_PLATFORM_STUCK_TEARDOWN_SECONDS", "2")
    monkeypatch.setattr(teardown, "_reachable", lambda sid: False)
    notes = []

    def state_reader(_config):
        return inventory.build_inventory([], registry.list_sessions(), activity={}).to_dict()

    detector = detect.Detector(
        config.load(), state_reader=state_reader,
        notifier=lambda note, **kwargs: notes.append((note, kwargs)),
    )
    assert detector.tick_once() == []
    clock["now"] += timedelta(seconds=1)
    assert detector.tick_once() == []
    clock["now"] += timedelta(seconds=1)
    signals = detector.tick_once()
    assert [item.kind for item in signals] == ["stuck_teardown"]
    assert state_reader(None)["runs"][0]["state"] == "stuck_teardown"
    assert len(notes) == 1
    assert session_id in notes[0][0]
    assert "stuck teardown" in notes[0][0]
    assert notes[0][1]["kind"] == "stuck_teardown"
    assert notes[0][1]["data"]["session"] == session_id
    assert detector.tick_once() == []
    assert len(notes) == 1


def test_timer_maturing_on_first_tick_still_produces_digest(ending, clock, monkeypatch):
    session_id, _, _ = ending
    registry.update(session_id, teardown={"unreachable_since": "2026-01-01T00:00:00Z"})
    clock["now"] += timedelta(seconds=60)
    monkeypatch.setattr(teardown, "_reachable", lambda sid: False)
    notes = []
    detector = detect.Detector(
        config.load(),
        state_reader=lambda _: inventory.build_inventory([], registry.list_sessions(), activity={}).to_dict(),
        notifier=lambda note, **kwargs: notes.append(note),
    )
    assert [item.kind for item in detector.tick_once()] == ["stuck_teardown"]
    assert len(notes) == 1 and session_id in notes[0]
    assert detector.tick_once() == []
    assert len(notes) == 1


def test_two_stuck_sessions_of_one_run_have_distinct_digest_keys():
    rows = [{
        "host": "gitlab.example.com", "project": "group/project", "slug": "same-run",
        "state": "stuck_teardown", "session": {"id": session},
        "attention": {"reason": "stuck_teardown", "since": "2026-01-01T00:00:00Z"},
    } for session in ("first", "second")]
    signals = detect._signals({"runs": rows})
    assert len(signals) == 2
    assert {item.data()["session"] for item in signals.values()} == {"first", "second"}


def test_stuck_state_also_wins_over_completed_or_unreadable_run(ending, clock, monkeypatch):
    session_id, _, _ = ending
    monkeypatch.setattr(teardown, "_reachable", lambda sid: False)
    teardown.sweep(1)
    clock["now"] += timedelta(seconds=1)
    teardown.sweep(1)
    entry = {**registry.read_session(session_id), "live": True}
    for unreadable in (None, "bad state"):
        state, attention = inventory._derive({"status": "complete"}, entry, unreadable=unreadable)
        assert state == "stuck_teardown"
        assert attention.reason == "stuck_teardown"
