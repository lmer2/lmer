"""Recovery uses real child exits, durable stores and recreated consumers."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import errno
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from lmer_cli.connector_lifecycle import ConnectorLifecycle
from lmer_platform import assistant, assistant_history, checkin, comms, config, daemon, detect
from lmer_platform import projects, registry, runs, spawn, store
from lmer_platform import project_context as scope
from tests.test_platform_projects import fleet  # noqa: F401
from tests.test_platform_checkin import iso, row, stamp


def test_supervisor_shutdown_waits_for_an_admitted_launch_to_publish(fleet, monkeypatch):
    projects.migrate()
    project = projects.create("Shutdown")
    entered, release, stopping = threading.Event(), threading.Event(), threading.Event()
    start = assistant.start

    def held_start(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return start(*args, **kwargs)

    monkeypatch.setattr(assistant, "start", held_start)
    supervisor = projects.Supervisor(fleet)
    supervisor.start()

    def stop():
        stopping.set()
        supervisor.stop()

    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            assert entered.wait(5)
            stopped = pool.submit(stop)
            assert stopping.wait(5)
            with pytest.raises(TimeoutError):
                stopped.result(timeout=.1)
        finally:
            release.set()
            supervisor.stop()
        stopped.result(timeout=5)
    assert not supervisor._thread.is_alive()
    state = projects.status(project["id"])
    assert state["running"] and state["generation"] == 1


def test_crash_recovery_is_bounded_and_does_not_drain_another_project(fleet):
    projects.migrate()
    alpha, beta = [projects.create(name) for name in ("Alpha", "Beta")]
    projects.reconcile(fleet)
    beta_session = projects.status(beta["id"])["session_id"]
    for project in (alpha, beta):
        with scope.bound(project["id"]):
            assistant.notify(project["name"] + " pending")
    for attempt in range(3):
        before = projects.status(alpha["id"])
        os.killpg(registry.read_session(before["session_id"])["pid"], signal.SIGKILL)
        assert spawn.wait_for_exit_recorded(before["session_id"])
        projects.migrate()
        projects.reconcile(fleet)
        if attempt < 2:
            assert projects.status(alpha["id"])["lifecycle"] == "waiting"
            with projects.database() as db:
                db.execute("UPDATE projects SET retry_at=0 WHERE id=?", (alpha["id"],))
            projects.reconcile(fleet)
            assert projects.status(alpha["id"])["generation"] == attempt + 2
    failed = projects.status(alpha["id"])
    assert failed["lifecycle"] == "failed"
    assert failed["pending"] == 1
    assert projects.status(beta["id"])["session_id"] == beta_session
    with scope.bound(beta["id"]):
        assert [note["note"] for note in assistant.take_pending()] == ["Beta pending"]


def test_restart_finishes_stop_intent_before_admitting_fifo_waiter(fleet):
    projects.migrate()
    config.update_stored({"max_live_assistants": 1})
    alpha, beta = [projects.create(name) for name in ("Alpha", "Beta")]
    projects.reconcile(fleet)
    old = projects.status(alpha["id"])["session_id"]
    with projects.database() as db:
        db.execute("UPDATE projects SET desired='stopped',status='stopping' WHERE id=?", (alpha["id"],))
    projects.reconcile(fleet)
    assert spawn.wait_for_exit_recorded(old)
    assert projects.status(alpha["id"])["lifecycle"] == "stopped"
    assert projects.status(beta["id"])["running"]


def test_general_migration_maps_ended_history_and_rolls_back_on_failed_import(fleet, monkeypatch):
    old = assistant.start(fleet)
    assistant_history.retain(old.session_id, {"id": "history", "kind": "message", "text": "retained decision",
                                            "source": {"channel": "agent"}})
    assistant.rotate(fleet)
    snapshot = assistant.state_path().read_bytes()
    real = registry.list_sessions
    monkeypatch.setattr(registry, "list_sessions", lambda **kw: real(**kw) if kw else (_ for _ in ()).throw(RuntimeError("interrupted import")))
    with pytest.raises(RuntimeError, match="interrupted import"):
        projects.migrate()
    assert not projects.enabled()
    assert assistant.state_path().read_bytes() == snapshot
    with projects.database() as db:
        assert db.execute("SELECT count(*) FROM projects").fetchone()[0] == 0
    monkeypatch.setattr(registry, "list_sessions", real)
    projects.migrate()
    assert assistant.state_path().read_bytes() == snapshot
    assert projects.session_home(old.session_id) == "general"
    assert any(message["text"] == "retained decision" for message in assistant_history.page(old.session_id, since=0)["messages"])


def _message(identity, text, *, health=False, kind="message"):
    return {"id": identity, "seq": int(identity), "kind": kind, "text": text,
            "source": {"channel": "platform"}, "meta": {"notification_class": "connector_health"} if health else {}}


def test_hub_restart_move_and_duplicate_ingest_deliver_once_without_health(fleet):
    projects.migrate()
    alpha, beta = [projects.create(name) for name in ("Alpha", "Beta")]
    with scope.bound(alpha["id"]):
        tracked = runs.track("forge.test", "repo", "stopped")
    projects.bind_session("s-worker", alpha["id"], tracked.key)
    hub = comms.CommsHub()
    hub.add_consumer(daemon._assistant_consumer(hub))
    health = _message("1", "thread disconnected", health=True)
    assert hub._ingest("s-worker", health)
    assert hub.flush_digests() == 0
    assert projects.status(alpha["id"])["pending"] == 0
    question = _message("2", "Question needs a human", kind="question")
    assert hub._ingest("s-worker", question)
    projects.move(tracked.key, beta["id"], expected_project=alpha["id"])
    restarted = comms.CommsHub()
    restarted.add_consumer(daemon._assistant_consumer(restarted))
    assert restarted._ingest("s-worker", question)
    assert restarted.flush_digests() == 1
    assert restarted.flush_digests() == 0
    with scope.bound(alpha["id"]):
        assert assistant.take_pending() == []
    with scope.bound(beta["id"]):
        pending = assistant.take_pending()
    digests = [note for note in pending if note["kind"] == "session_comms"]
    assert len(digests) == 1
    assert digests[0]["data"]["count"] == 1
    assert "Question needs a human" in digests[0]["note"]
    assert "disconnected" not in digests[0]["note"]
    assert len(restarted.feed(0)[0]) == 2


def test_hourly_checkins_are_partitioned_without_losing_real_events(fleet):
    projects.migrate()
    alpha, beta = [projects.create(name) for name in ("Alpha", "Beta")]
    rows = []
    for project in (alpha, beta):
        item = row(project["name"])
        with scope.bound(project["id"]):
            runs.track(item["host"], item["project"], item["slug"])
        rows.append(item)
    payload = {"runs": rows}
    checkin.observe(payload)
    for item in rows:
        stamp(item["slug"], "first_seen", iso(hours=2))
    detector = detect.Detector(fleet)
    assert len(detector._check_ins(payload)) == 2
    assert detector._check_ins(payload) == []
    for project, other in ((alpha, beta), (beta, alpha)):
        with scope.bound(project["id"]):
            notes = assistant.take_pending()
        assert len(notes) == 1
        assert project["name"] in notes[0]["note"]
        assert other["name"] not in notes[0]["note"]


def test_connector_transition_is_typed_before_it_reaches_the_real_hub(fleet):
    projects.migrate()
    projects.bind_session("s-worker", "general")
    hub = comms.CommsHub()
    class Bus:
        def post(self, kind, text, **kwargs):
            message = {**_message("1", text, kind=kind), **kwargs}
            assert hub._ingest("s-worker", message)
    fixture = SimpleNamespace(bus=Bus(), _lease=SimpleNamespace(key=SimpleNamespace(transport="slack")), _notify=None)
    ConnectorLifecycle._transition_note(fixture, "connector retrying")
    assert hub.feed(0)[0][0].message["meta"]["notification_class"] == "connector_health"
    assert hub.flush_digests() == 0
    assert projects.status("general")["pending"] == 0


def test_assistant_does_not_execute_before_ownership_is_published(fleet, tmp_path, monkeypatch):
    projects.migrate()
    project = projects.create("Publication")
    marker = tmp_path / "executed"
    executable = tmp_path / "guarded-lmer"
    executable.write_text(f"#!/bin/sh\ntouch '{marker}'\nexec sleep 120\n")
    executable.chmod(0o755)
    settings = replace(fleet, lmer_bin=str(executable))
    entered, release = Event(), Event()
    real_bind = projects.bind_session
    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return real_bind(*args, **kwargs)
    monkeypatch.setattr(projects, "bind_session", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(projects.reconcile, settings)
        assert entered.wait(10)
        assert not marker.exists()
        release.set()
        future.result()
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert marker.exists()
    assert projects.status(project["id"])["running"]


def _process_running(proc):
    try:
        state = proc.read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state != "Z"


def _assert_child_exited_without_execution(pid, marker, *, timeout=5):
    proc = Path(f"/proc/{pid}/stat")
    deadline = time.monotonic() + timeout
    try:
        while _process_running(proc) and time.monotonic() < deadline:
            time.sleep(.02)
        assert not _process_running(proc)
        assert not marker.exists()
    finally:
        if _process_running(proc):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("read_index", [0, 1, 2], ids=["poll", "assertion", "cleanup"])
@pytest.mark.parametrize("error", [errno.ENOENT, errno.ESRCH])
def test_orphan_observation_accepts_process_disappearance(tmp_path, monkeypatch, read_index, error):
    proc = Path("/proc/4242/stat")
    read_text = Path.read_text
    reads = []

    def disappearing(path, *args, **kwargs):
        if path != proc:
            return read_text(path, *args, **kwargs)
        reads.append(path)
        if len(reads) - 1 == read_index:
            raise OSError(error, os.strerror(error), str(path))
        return "4242 (fixture) Z 1"

    monkeypatch.setattr(Path, "read_text", disappearing)
    _assert_child_exited_without_execution(4242, tmp_path / "not-executed")
    assert reads == [proc] * 3


@pytest.mark.parametrize("error", [PermissionError(errno.EACCES, "denied"), OSError(errno.EIO, "I/O error")])
def test_orphan_observation_does_not_hide_other_read_errors(error):
    def read_text():
        raise error

    with pytest.raises(type(error)) as raised:
        _process_running(SimpleNamespace(read_text=read_text))
    assert raised.value is error


def test_orphan_observation_distinguishes_running_and_exited_child(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
                             stdin=subprocess.PIPE, start_new_session=True)
    try:
        assert _process_running(Path(f"/proc/{child.pid}/stat"))
        child.stdin.close()
        _assert_child_exited_without_execution(child.pid, tmp_path / "not-executed")
        assert child.wait(timeout=5) == 0
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def test_orphan_observation_fails_and_kills_a_surviving_child(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
    try:
        with pytest.raises(AssertionError):
            _assert_child_exited_without_execution(child.pid, tmp_path / "not-executed", timeout=0)
        assert child.wait(timeout=5) == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def test_orphan_observation_still_refuses_an_executable_marker(tmp_path):
    marker = tmp_path / "executed"
    marker.touch()
    with pytest.raises(AssertionError):
        _assert_child_exited_without_execution(2 ** 30, marker)


def test_orphan_cleanup_tolerates_process_disappearance_before_signal(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(Path, "read_text", lambda _path: "4242 (fixture) S 1")

    def gone(pid, sig):
        calls.append((pid, sig))
        raise ProcessLookupError(errno.ESRCH, "No such process")

    monkeypatch.setattr(os, "killpg", gone)
    with pytest.raises(AssertionError):
        _assert_child_exited_without_execution(4242, tmp_path / "not-executed", timeout=0)
    assert calls == [(4242, signal.SIGKILL)]


def test_parent_death_in_publication_gap_cannot_launch_an_orphan_assistant(fleet, tmp_path):
    root = tmp_path / "crash-platform"
    marker = tmp_path / "orphan-executed"
    executable = tmp_path / "crash-lmer"
    executable.write_text(f"#!/bin/sh\ntouch '{marker}'\nexec sleep 120\n")
    executable.chmod(0o755)
    code = '''
import os, sys
from pathlib import Path
from lmer_platform import config, projects, registry, store
store.PLATFORM_DIR = sys.argv[1]
settings = config.load({"lmer_bin": sys.argv[2]})
config.ensure_secret(settings)
projects.migrate()
projects.create("Interrupted")
def crash(*args, **kwargs):
    Path(sys.argv[3]).write_text(str(kwargs["pid"]))
    os._exit(77)
registry.register = crash
projects.reconcile(settings)
'''
    pid_file = tmp_path / "launch-pid"
    result = subprocess.run([sys.executable, "-c", code, str(root), str(executable), str(pid_file)], timeout=20)
    assert result.returncode == 77
    pid = int(pid_file.read_text())
    _assert_child_exited_without_execution(pid, marker)


def test_guarded_exec_failure_retains_actual_error_and_can_retry(fleet, tmp_path):
    projects.migrate()
    project = projects.create("Exec failure")
    projects.reconcile(replace(fleet, lmer_bin=str(tmp_path / "missing-lmer")))
    status = projects.status(project["id"])
    assert status["lifecycle"] == "failed"
    assert "No such file or directory" in status["error"]
    assert "missing-lmer" in status["error"]
    assert not status["running"]
    projects.request_start(project["id"])
    projects.reconcile(fleet)
    assert projects.status(project["id"])["running"]


def test_mixed_health_question_failure_and_completion_batches_keep_only_real_work(fleet):
    projects.migrate()
    alpha, beta = [projects.create(name) for name in ("Alpha", "Beta")]
    hub = comms.CommsHub()
    hub.add_consumer(daemon._assistant_consumer(hub))
    for project in (alpha, beta):
        session = "s-" + project["id"]
        projects.bind_session(session, project["id"])
        for message in (_message("1", "health-only retry", health=True),
                        _message("2", project["name"] + " question", kind="question"),
                        _message("3", project["name"] + " failed validation", kind="milestone"),
                        _message("4", project["name"] + " completed review", kind="milestone")):
            message["id"] = f"{session}/{message['id']}"
            assert hub._ingest(session, message)
    assert hub.flush_digests() == 2
    for project, other in ((alpha, beta), (beta, alpha)):
        with scope.bound(project["id"]):
            notes = assistant.take_pending()
        assert len(notes) == 1
        assert notes[0]["data"]["count"] == 3
        assert all(word in notes[0]["note"] for word in ("question", "failed validation", "completed review"))
        assert "health-only" not in notes[0]["note"]
        assert other["name"] not in notes[0]["note"]
    assert len([event for event in store.read_events() if event["type"] == detect.SESSION_SIGNALLED]) == 4
