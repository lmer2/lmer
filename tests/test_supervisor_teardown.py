"""Process/PTY regressions for shutdown, independent of a particular harness."""
import contextlib
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from lmer_cli import comms, supervisor
from tests.conftest import strip_lmer_env
from tests.timing_budgets import (
    BUSY_BOUND, CHILD_ALIVE, HANDOFF, JOIN, LATE_HANDOFF, SHORT_CHILD,
    TEARDOWN_BOUND, XDIST_GROUP, outlast,
)

pytestmark = pytest.mark.xdist_group(XDIST_GROUP)


NATURAL_EXIT_CHILD = f"""
import os, signal, time
go = False
def begin_exit(*args):
    global go
    go = True
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGUSR2, begin_exit)
os.write(1, b'ready\\n')
while not go:
    time.sleep(.005)
time.sleep({SHORT_CHILD})
"""


@pytest.fixture(autouse=True)
def clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def supervised(monkeypatch, tmp_path):
    log = supervisor._open_comms_log(str(tmp_path), env={"LMER_SESSION_ID": "20260909-120000-42-123"})
    observed = {"api_closed": threading.Event()}
    children = []
    ptys = []
    fork = os.fork
    openpty = os.openpty
    build = supervisor._build_fastapi_app
    monkeypatch.setattr(supervisor, "END_REQUEST_HANDOFF_SECONDS", HANDOFF, raising=False)

    def track_fork():
        pid = fork()
        if pid:
            children.append(pid)
            observed["child_pid"] = pid
        return pid

    def track_pty():
        pair = openpty()
        ptys.extend(pair)
        return pair

    def build_app(*args, **kwargs):
        observed["geometry"] = kwargs["get_winsize"]
        observed["output"] = args[0]
        observed["app"] = build(args[0], args[1], "teardown-control-test-key", comms=log,
                                agent_token="teardown-agent-test-key")
        return observed["app"]

    def start_server(*args):
        observed["server_started"] = True

        def stop_server():
            try:
                status = os.waitid(os.P_PID, children[-1], os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                observed["child_running_at_stop"] = False
            else:
                observed["child_running_at_stop"] = status is None
            observed["final_geometry"] = observed["geometry"]()
            observed["api_closed"].set()

        return None, stop_server

    def evidence():
        """Render the supervised child state and captured output for a failed wait."""
        buffer = observed.get("output")
        output = buffer.read_since(0, timeout=0)[0] if buffer is not None else b""
        return "child pid=%s exit=%s running_at_stop=%s\n%s" % (
            observed.get("child_pid"), observed.get("exit_code"),
            observed.get("child_running_at_stop"), output.decode(errors="replace"))

    observed["evidence"] = evidence
    monkeypatch.setattr(supervisor.os, "fork", track_fork)
    monkeypatch.setattr(supervisor.os, "openpty", track_pty)
    monkeypatch.setattr(supervisor, "_open_comms_log", lambda: log)
    monkeypatch.setattr(supervisor, "_open_ask_projection", lambda _: None)
    monkeypatch.setattr(supervisor.SessionLog, "open_if_mounted", lambda: None)
    monkeypatch.setattr(supervisor, "_resolve_fastapi_port", lambda *args: 0)
    monkeypatch.setattr(supervisor, "_build_fastapi_app", build_app)
    monkeypatch.setattr(supervisor, "_start_fastapi_server", start_server)

    def run(code, *, driver=None, host_size=None):
        stdin_read, stdin_write = os.pipe() if host_size is None else openpty()[::-1]
        if host_size is not None:
            supervisor._set_winsize(stdin_read, *host_size)
        output = os.open(os.devnull, os.O_WRONLY)
        previous_handler = signal.getsignal(signal.SIGUSR1)
        driver_errors = []

        def drive():
            try:
                deadline = time.monotonic() + JOIN
                while signal.getsignal(signal.SIGUSR1) is previous_handler:
                    assert time.monotonic() < deadline, "shutdown handler was never installed"
                    time.sleep(0.005)
                driver(stdin_read, observed)
            except BaseException as exc:
                driver_errors.append(exc)

        thread = threading.Thread(target=drive) if driver else None
        if thread:
            thread.start()
        started = time.monotonic()
        try:
            observed["exit_code"] = supervisor.run_supervisor(
                [sys.executable, "-c", code],
                {"fastapi": True, "manual_start": True, "host": "127.0.0.1",
                 "token": "", "winsize_recheck_delay": 0},
                stdin_fd=stdin_read, stdout_fd=output,
            )
            observed["duration"] = time.monotonic() - started
            assert observed.get("server_started"), "test missed the control server seam"
            return observed, log
        finally:
            if thread:
                thread.join(timeout=JOIN)
                assert not thread.is_alive()
            # Production exits this process; the fixture reuses its fd table.
            # Finish delayed chords before another test can open a new PTY.
            for name in ("lmer-supervisor-self-shutdown", "lmer-supervisor-quit"):
                for worker in threading.enumerate():
                    if worker.name == name:
                        worker.join(timeout=JOIN)
                        assert not worker.is_alive()
            for pid in children:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pid, signal.SIGKILL)
                with contextlib.suppress(ChildProcessError):
                    os.waitpid(pid, 0)
            for fd in (stdin_read, stdin_write, output, *ptys):
                with contextlib.suppress(OSError):
                    os.close(fd)
            assert not driver_errors, driver_errors

    return run


def test_control_stays_up_after_pty_eof_until_child_exit(supervised):
    result, _ = supervised(
        f"import os,time; os.close(0); os.close(1); os.close(2); time.sleep({SHORT_CHILD})"
    )
    assert result["child_running_at_stop"] is False
    assert result["exit_code"] == 0


def test_reaps_harness_without_waiting_for_descendants_pty_eof(supervised):
    result, _ = supervised(
        "import os,signal,time; signal.signal(signal.SIGHUP, signal.SIG_IGN); "
        f"child=os.fork(); time.sleep({CHILD_ALIVE}) if child == 0 else None; os._exit(0)"
    )
    assert result["child_running_at_stop"] is False
    assert result["duration"] < TEARDOWN_BOUND, "supervisor waited for the descendant's PTY EOF"


@pytest.mark.parametrize("size", [(0, 0), (24, 1), (1, 80)])
def test_transient_host_geometry_preserves_last_valid_size(supervised, size):
    def resize(fd, _observed):
        supervisor._set_winsize(fd, *size)
        os.kill(os.getpid(), signal.SIGWINCH)

    result, _ = supervised(f"import time; time.sleep({SHORT_CHILD})", driver=resize, host_size=(24, 80))
    assert result["final_geometry"] == (24, 80)


def test_headless_child_starts_with_readable_geometry(supervised):
    result, _ = supervised("pass")
    assert result["final_geometry"] == (24, 80)


def test_requested_shutdown_publishes_structured_ack_and_reaps(supervised):
    def request(_fd, observed):
        output, _, _ = observed["output"].read_since(0, timeout=JOIN)
        assert b"ready" in output, "child did not reach exec before the quit chord\n" + observed["evidence"]()
        os.kill(os.getpid(), signal.SIGUSR1)

    result, log = supervised(f"import time; print('ready', flush=True); time.sleep({CHILD_ALIVE})", driver=request)
    messages, _ = log.read_since(0)
    acknowledgments = [m for m in messages if m.meta.get("event") == comms.EVENT_SESSION_END_REQUESTED]
    assert len(acknowledgments) == 1
    assert acknowledgments[0].source.channel == comms.CHANNEL_SUPERVISOR
    assert result["child_running_at_stop"] is False
    assert result["exit_code"] == 0


def test_shutdown_without_daemon_stays_bounded_and_unarmed(supervised, monkeypatch, tmp_path):
    from lmer_platform import registry, store, teardown

    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    session_id = registry.new_session_id()
    registry.register(session_id, pid=os.getpid())

    def request(_fd, observed):
        output, _, _ = observed["output"].read_since(0, timeout=JOIN)
        assert b"ready" in output, observed["evidence"]()
        os.kill(os.getpid(), signal.SIGUSR1)

    result, log = supervised(f"import time; print('ready', flush=True); time.sleep({CHILD_ALIVE})", driver=request)
    assert result["exit_code"] == 0
    assert HANDOFF <= result["duration"] < TEARDOWN_BOUND
    assert log.subscribers() == {}
    messages, _ = log.read_since(0)
    assert any(m.meta.get("event") == comms.EVENT_SESSION_END_REQUESTED for m in messages)
    assert not registry.read_session(session_id).get("teardown_requested_at")
    samples = []
    monkeypatch.setattr(teardown, "_reachable", lambda sid: samples.append(sid) or False)
    now = datetime.now(timezone.utc)
    assert teardown.sweep(60, now=now) == []
    assert teardown.sweep(60, now=now + timedelta(seconds=60)) == []
    assert samples == []


def test_delivery_requires_a_specific_authenticated_daemon_ack():
    log = comms.MessageLog("work-session")
    message = supervisor._announce_shutdown(log)
    app = supervisor._build_fastapi_app(
        supervisor.OutputBuffer(), lambda data: len(data), "control-test-key",
        agent_token="agent-test-key", comms=log,
    )
    with TestClient(app) as client:
        payload = {"subscriber": comms.PLATFORM_SUBSCRIBER, "seq": message.seq}
        assert not log.wait_acknowledged(comms.PLATFORM_SUBSCRIBER, message.seq, 0)
        response = client.post("/messages/ack", json=payload,
                               headers={"Authorization": "Bearer agent-test-key"})
        assert response.status_code == 403
        assert not log.wait_acknowledged(comms.PLATFORM_SUBSCRIBER, message.seq, 0)
        headers = {"Authorization": "Bearer control-test-key"}
        for body in ({**payload, "seq": 0}, {**payload, "subscriber": "other-client"}):
            assert client.post("/messages/ack", json=body, headers=headers).status_code == 200
            assert not log.wait_acknowledged(comms.PLATFORM_SUBSCRIBER, message.seq, 0)
        assert client.post("/messages/ack", json={**payload, "final": True},
                           headers=headers).status_code == 200
        assert log.wait_acknowledged(comms.PLATFORM_SUBSCRIBER, message.seq, 0)


@pytest.mark.parametrize("confirmed", [False, True])
def test_handoff_logs_once_only_when_delivery_is_unconfirmed(confirmed, caplog):
    log = comms.MessageLog("handoff-test-session")
    handoff = supervisor._ShutdownHandoff(log, HANDOFF)
    started = time.monotonic()
    handoff.start()
    if confirmed:
        messages, _ = log.read_since(0, timeout=JOIN)
        log.ack(comms.PLATFORM_SUBSCRIBER, messages[-1].seq)
    handoff.wait()
    assert handoff.done.wait(JOIN)
    handoff.wait()
    assert time.monotonic() - started < outlast(HANDOFF)
    records = [r for r in caplog.records if "supervisor_end_handoff_unconfirmed" in r.message]
    assert len(records) == (0 if confirmed else 1)
    if not confirmed:
        assert "session=handoff-test-session" in records[0].message


def test_stalled_publication_cannot_hold_natural_exit_forever(supervised, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    published = threading.Event()
    announce = supervisor._announce_shutdown

    def stalled(log):
        entered.set()
        try:
            assert release.wait(JOIN)
            return announce(log)
        finally:
            published.set()

    monkeypatch.setattr(supervisor, "_announce_shutdown", stalled)

    def request(_fd, observed):
        output, _, _ = observed["output"].read_since(0, timeout=JOIN)
        assert b"ready" in output, observed["evidence"]()
        os.kill(os.getpid(), signal.SIGUSR1)
        assert entered.wait(JOIN), observed["evidence"]()
        os.kill(observed["child_pid"], signal.SIGUSR2)

    try:
        result, _ = supervised(NATURAL_EXIT_CHILD, driver=request)
        assert result["exit_code"] == 0
        assert result["duration"] < TEARDOWN_BOUND
        assert result["api_closed"].is_set()
    finally:
        release.set()
        if entered.is_set():
            assert published.wait(JOIN)


def test_natural_exit_waits_for_authenticated_late_subscriber(supervised, monkeypatch, tmp_path):
    from lmer_platform import comms as platform_comms
    from lmer_platform import config, detect, inventory, registry, store, teardown

    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    store.write_json(config.config_path(), {"nudge_after_seconds": 0, "checkin_window_seconds": 0})
    # Exceed TEARDOWN_BOUND so waiting out the handoff would fail the duration assertion.
    monkeypatch.setattr(supervisor, "END_REQUEST_HANDOFF_SECONDS", LATE_HANDOFF, raising=False)
    session_id = registry.new_session_id()
    registry.register(session_id, pid=os.getpid())
    hub = platform_comms.CommsHub()
    publish_entered = threading.Event()
    release_publish = threading.Event()
    published = threading.Event()
    announce = supervisor._announce_shutdown

    def delayed_announce(log):
        publish_entered.set()
        try:
            assert release_publish.wait(JOIN)
            return announce(log)
        finally:
            published.set()

    monkeypatch.setattr(supervisor, "_announce_shutdown", delayed_announce)

    def request(_fd, observed):
        output, _, _ = observed["output"].read_since(0, timeout=JOIN)
        assert b"ready" in output, observed["evidence"]()
        os.kill(os.getpid(), signal.SIGUSR1)
        try:
            assert publish_entered.wait(JOIN), observed["evidence"]()
            os.kill(observed["child_pid"], signal.SIGUSR2)
            deadline = time.monotonic() + JOIN
            while True:
                try:
                    os.waitid(os.P_PID, observed["child_pid"], os.WEXITED | os.WNOHANG | os.WNOWAIT)
                except ChildProcessError:
                    break
                assert time.monotonic() < deadline, "natural exit was not reaped"
                time.sleep(.005)
            time.sleep(.05)
            assert not observed["api_closed"].is_set(), "API closed before pending acknowledgment delivery"
            release_publish.set()
            assert published.wait(JOIN)
            with TestClient(observed["app"]) as client:
                headers = {"Authorization": "Bearer teardown-control-test-key"}
                response = client.get("/messages", headers=headers)
                assert response.status_code == 200
                messages = response.json()["messages"]
                assert any(m.get("meta", {}).get("event") == comms.EVENT_SESSION_END_REQUESTED
                           for m in messages)
                for message in messages:
                    assert hub._ingest(session_id, message)
                response = client.post("/messages/ack", headers=headers,
                                       json={"subscriber": platform_comms.SUBSCRIBER,
                                             "seq": response.json()["cursor"]})
                assert response.status_code == 200
        finally:
            release_publish.set()
            assert published.wait(JOIN)

    result, _ = supervised(NATURAL_EXIT_CHILD, driver=request)
    assert result["exit_code"] == 0
    assert result["child_running_at_stop"] is False
    assert result["duration"] < TEARDOWN_BOUND, "confirmed delivery did not release the handoff"
    assert registry.read_session(session_id)["teardown_requested_at"]
    now = {"value": datetime.now(timezone.utc)}
    original_sweep = teardown.sweep
    monkeypatch.setattr(teardown, "sweep", lambda grace: original_sweep(grace, now=now["value"]))
    monkeypatch.setattr(teardown, "_reachable", lambda sid: False)
    notes = []
    detector = detect.Detector(
        config.load(),
        state_reader=lambda _: inventory.build_inventory([], registry.list_sessions(), activity={}).to_dict(),
        notifier=lambda note, **kwargs: notes.append(note),
    )
    assert detector.tick_once() == []
    now["value"] += timedelta(seconds=60)
    assert [item.kind for item in detector.tick_once()] == ["stuck_teardown"]
    assert len(notes) == 1 and session_id in notes[0]


@pytest.mark.parametrize("wait", ["drained", "acknowledged"])
def test_delivery_wait_bound_includes_a_busy_bus(wait):
    log = comms.MessageLog("work-session")
    held = threading.Event()
    release = threading.Event()

    def busy_writer():
        with log._lock:
            held.set()
            release.wait(outlast(BUSY_BOUND))

    writer = threading.Thread(target=busy_writer)
    writer.start()
    try:
        assert held.wait(JOIN)
        started = time.monotonic()
        delivered = (log.wait_drained(.02) if wait == "drained" else
                     log.wait_acknowledged(comms.PLATFORM_SUBSCRIBER, 1, .02))
        assert time.monotonic() - started < BUSY_BOUND
        assert delivered is False
    finally:
        release.set()
        writer.join(timeout=JOIN)
        assert not writer.is_alive()


def test_real_child_ignoring_quit_and_term_is_killed_and_reaped(supervised, monkeypatch):
    original_shutdown = supervisor._self_shutdown
    original_kill = os.kill
    signals = []

    def shutdown(write, pid, **kwargs):
        return original_shutdown(write, pid, chord_gap=0, escalate_grace=.05, **kwargs)

    def kill(pid, sig):
        if pid != os.getpid() and sig in (signal.SIGTERM, signal.SIGKILL):
            signals.append(sig)
        original_kill(pid, sig)

    monkeypatch.setattr(supervisor, "_self_shutdown", shutdown)
    monkeypatch.setattr(supervisor.os, "kill", kill)

    def request(_fd, observed):
        output, _, _ = observed["output"].read_since(0, timeout=JOIN)
        assert b"ready" in output, observed["evidence"]()
        os.kill(os.getpid(), signal.SIGUSR1)

    result, _ = supervised(
        "import signal,time; signal.signal(signal.SIGINT, signal.SIG_IGN); "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); "
        f"time.sleep({CHILD_ALIVE})",
        driver=request,
    )
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert result["child_running_at_stop"] is False
    assert result["exit_code"] == 0


def test_blocked_quit_write_cannot_hold_escalation(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    writer_done = threading.Event()
    escalated = threading.Event()
    killed = []

    def write(data):
        entered.set()
        try:
            release.wait(JOIN)
            return len(data)
        finally:
            writer_done.set()

    def kill(pid, sig):
        killed.append(sig)
        escalated.set()

    monkeypatch.setattr(supervisor, "_wait_child_exit", lambda *args: False)
    monkeypatch.setattr(supervisor.os, "kill", kill)
    worker = threading.Thread(target=supervisor._self_shutdown, args=(write, 12345),
                              kwargs={"quit_sequence": (b"q",), "chord_gap": 0})
    worker.start()
    try:
        assert entered.wait(JOIN), "test did not reach the blocked writer"
        escalated_while_blocked = escalated.wait(JOIN)
    finally:
        release.set()
        worker.join(timeout=JOIN)
        assert writer_done.wait(JOIN)
        assert not worker.is_alive()
    assert escalated_while_blocked
    assert killed == [signal.SIGTERM, signal.SIGKILL]
