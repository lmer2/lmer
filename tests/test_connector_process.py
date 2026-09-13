"""Real connector children, real supervisor sockets, and joined ownership lifetimes."""
import json
import time

import pytest

from lmer_cli import comms
from lmer_cli.connector_delivery import RecoveryRequired, checkpoint_path, marker_path
from lmer_cli.connector_ownership import Lease, OwnershipBusy, read_json
from tests.connector_fakes import ChildHarness, isolated_connectors, live_supervisor


def wait_for(predicate, harness, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    import signal
    for process in harness.children:
        if process.poll() is None:
            process.send_signal(signal.SIGUSR1)
    time.sleep(0.1)
    from pathlib import Path
    logs = [Path(path) for path in harness.log_paths]
    output = "\n".join(str(path) + "\n" + path.read_text() for path in logs if path.exists())
    pytest.fail(output or "child condition timed out")


@pytest.fixture(params=["slack", "matrix"])
def child(request, tmp_path, live_supervisor):
    harness = ChildHarness(tmp_path / "child", request.param, live_supervisor)
    try:
        yield harness
    finally:
        harness.close()


def test_actual_startup_send_and_exclusive_child_handoff(child):
    record, _ = child.supervisor.log.append(kind="milestone", text="real child send", source=comms.Source("agent"))
    owned = child.spawn()
    try:
        wait_for(lambda: bool(child.events()), child)
        assert len(child.events()) == 1
        with pytest.raises(OwnershipBusy):
            child.spawn(owner="platform")
        assert len(child.children) == 1
        assert child.supervisor.log.read_since(0)[0][0].id == record.id
    finally:
        owned.stop(timeout=30)
    assert owned.popen.returncode is not None
    lease = Lease.acquire(child.key, owner="next")
    lease.close()


def test_actual_verified_rejection_retries_without_duplicate(child):
    (child.root / "mode").write_text("rejection")
    child.supervisor.log.append(kind="milestone", text="accepted once", source=comms.Source("agent"))
    owned = child.spawn()
    try:
        wait_for(lambda: bool(child.events()), child)
        wait_for(lambda: read_json(checkpoint_path(child.key, child.supervisor.log.session))["transport"]["_bus_cursor"] >= 1, child)
        assert sum("accepted once" in event["text"] for event in child.events()) == 1
        assert not marker_path(child.key).exists()
    finally:
        owned.stop(timeout=30)


@pytest.mark.parametrize("mode", ["unknown", "receipt-failure"])
def test_confirmed_remote_send_barrier_survives_exit_and_owner_reconstruction(child, mode):
    (child.root / "mode").write_text(mode)
    record, _ = child.supervisor.log.append(kind="milestone", text="blocked receipt", source=comms.Source("agent"))
    owned = child.spawn()
    wait_for(lambda: owned.popen.poll() is not None, child)
    assert owned.collect() == 4
    assert len(child.events()) == 1
    marker = marker_path(child.key).read_bytes()
    checkpoint = checkpoint_path(child.key, child.supervisor.log.session).read_bytes()
    for category in ("platform", "direct"):
        with pytest.raises(RecoveryRequired):
            child.spawn(owner=category)
    assert marker_path(child.key).read_bytes() == marker
    assert checkpoint_path(child.key, child.supervisor.log.session).read_bytes() == checkpoint
    assert child.supervisor.log.read_since(0)[0][0].id == record.id
    assert len(child.events()) == 1


def test_module_change_execs_under_same_lease_without_replaying(child):
    child.supervisor.log.append(kind="milestone", text="before reload", source=comms.Source("agent"))
    owned = child.spawn()
    try:
        wait_for(lambda: len(child.events()) == 1, child)
        old_token = read_json(child.key.owner_path)["token"]
        (child.root / "watched.py").write_text("# changed module\n")
        wait_for(lambda: (child.root / "execs").exists(), child)
        child.supervisor.log.append(kind="milestone", text="after reload", source=comms.Source("agent"))
        wait_for(lambda: any("after reload" in x["text"] for x in child.events()), child)
        assert read_json(child.key.owner_path)["token"] == old_token
        assert (child.root / "execs").read_text().strip() == str(owned.popen.pid)
        assert sum("before reload" in event["text"] for event in child.events()) == 1
        with pytest.raises(OwnershipBusy):
            child.spawn(owner="platform")
    finally:
        owned.stop(timeout=30)


def test_module_change_bounds_drain_and_keeps_uncertain_send_blocked(child):
    (child.root / "mode").write_text("wait-after-send")
    child.supervisor.log.append(kind="milestone", text="in flight", source=comms.Source("agent"))
    owned = child.spawn()
    wait_for(lambda: (child.root / "wait-after-send.reached").exists(), child)
    started = time.monotonic()
    (child.root / "watched.py").write_text("# reload during blocked send\n")
    wait_for(lambda: owned.popen.poll() is not None, child, timeout=60)
    assert time.monotonic() - started < 30
    assert owned.collect() == 4
    assert len(child.events()) == 1
    assert marker_path(child.key).exists()
    assert not (child.root / "execs").exists()
    with pytest.raises(RecoveryRequired):
        child.spawn()


def test_direct_owner_restarts_only_failed_connector_and_collects_every_child(child, monkeypatch):
    from lmer_cli import connector_process, thread_access
    from tests.test_platform_thread_connectors import FakeSlack, FakeMatrix

    for key, value in child.environment().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(thread_access, "_slack_client", lambda *args: FakeSlack())
    monkeypatch.setattr(thread_access, "_matrix_client", lambda *args: FakeMatrix())
    owner = connector_process.ConnectorProcess(child.channel, child.key.conversation, child.key.root,
                                              child.link, (child.supervisor.endpoint.url, child.supervisor.endpoint.token),
                                              runner=child.runner)
    (child.root / "mode").write_text("inbound-fail")
    session = child.supervisor.log.session
    owner.start()
    try:
        wait_for(lambda: len(child.children) >= 1 and child.children[0].poll() is not None, child)
        (child.root / "mode").write_text("healthy")
        child.supervisor.log.append(kind="milestone", text="after child restart", source=comms.Source("agent"))
        wait_for(lambda: len(child.children) >= 2 and any("after child restart" in x["text"] for x in child.events()), child)
        assert child.children[0].returncode in (1, 3)
        assert child.children[-1].poll() is None
        assert child.supervisor.client.health()["session"] == session
    finally:
        owner.stop()
    assert not owner._thread.is_alive()
    assert all(process.returncode is not None for process in child.children)


def test_platform_owner_reconstructs_without_replaying_blocked_send(child, monkeypatch):
    import os
    from lmer_platform import connectors, registry as sessions, spawn
    from tests.test_platform_thread_connectors import FakeSlack, FakeMatrix

    session = "s-20260910T154705-ea5e"
    supervisor_session = child.supervisor.log.session
    port = int(child.supervisor.endpoint.url.rsplit(":", 1)[1])
    sessions.register(session, kind="worker", pid=os.getpid(), control={
        "host": "127.0.0.1", "port": port, "token_ref": str(spawn.token_file_for(session))})
    spawn.token_file_for(session).write_text(child.supervisor.endpoint.token)
    def component():
        return connectors.ThreadConnectors(runner=child.runner, environ=child.environment(),
                                           slack_client=lambda *args: FakeSlack(),
                                           matrix_client=lambda *args: FakeMatrix(), clock=lambda: 1000)
    first = component()
    again = None
    (child.root / "mode").write_text("receipt-failure")
    try:
        assert first.connect(session, child.link, by="test operator")["running"]
        wait_for(lambda: checkpoint_path(child.key, supervisor_session).exists(), child)
        child.supervisor.log.append(kind="milestone", text="platform confirmed send", source=comms.Source("agent"))
        wait_for(lambda: child.children[0].poll() is not None, child)
        first.reconcile()
        first.stop()
        again = component()
        again.reconcile()
        assert len(child.children) == 1
        assert not again.listing(session)[0]["running"]
        assert "recovery" in again.listing(session)[0]["detail"]
        assert len(child.events()) == 1
        assert child.supervisor.client.health()["session"] == supervisor_session
    finally:
        first.stop()
        if again:
            again.stop()
    assert all(process.returncode is not None for process in child.children)


@pytest.mark.parametrize("deferred_identity", [False, True])
def test_platform_registry_identity_is_not_the_supervisor_identity(child, monkeypatch, deferred_identity):
    import os
    from lmer_platform import connectors, registry as sessions, spawn
    from tests.test_platform_thread_connectors import FakeSlack, FakeMatrix

    session = "s-20260910T154705-ea5e"
    supervisor_session = child.supervisor.log.session
    port = int(child.supervisor.endpoint.url.rsplit(":", 1)[1])
    sessions.register(session, kind="worker", pid=os.getpid(), control={
        "host": "127.0.0.1", "port": port, "token_ref": str(spawn.token_file_for(session))})
    spawn.token_file_for(session).write_text(child.supervisor.endpoint.token)
    health = connectors.session_io.quiet_health
    if deferred_identity:
        monkeypatch.setattr(connectors.session_io, "quiet_health", lambda sid: None)

    def component():
        return connectors.ThreadConnectors(runner=child.runner, environ=child.environment(),
                                           slack_client=lambda *args: FakeSlack(),
                                           matrix_client=lambda *args: FakeMatrix(), clock=lambda: 1000)

    first, again = component(), None
    try:
        first.connect(session, child.link, by="test operator")
        wait_for(lambda: checkpoint_path(child.key, supervisor_session).exists()
                 or child.children[0].poll() is not None, child)
        assert child.children[0].poll() is None, child.log_paths[0].read_text()
        assert read_json(child.key.owner_path)["session"] == supervisor_session
        child.supervisor.log.append(kind="milestone", text="before restart", source=comms.Source("agent"))
        wait_for(lambda: len(child.events()) == 1, child)
        first.stop()
        monkeypatch.setattr(connectors.session_io, "quiet_health", health)
        again = component()
        again.reconcile()
        child.supervisor.log.append(kind="milestone", text="after restart", source=comms.Source("agent"))
        wait_for(lambda: len(child.events()) >= 2, child)
        messages = [event["text"] for event in child.events()]
        assert len(messages) == 2
        assert messages[0].endswith("before restart")
        assert messages[1].endswith("after restart")
        assert read_json(child.key.owner_path)["session"] == supervisor_session
        assert again.listing(session)[0]["running"]
        assert not checkpoint_path(child.key, session).exists()
    finally:
        first.stop()
        if again:
            again.stop()
    assert all(process.returncode is not None for process in child.children)


ORPHAN_PROBE = r'''
import ctypes
import json
import os
from pathlib import Path
import signal
import sys
import time
from lmer_cli.connector_ownership import ThreadKey, OwnershipBusy, read_json, write_json
from lmer_cli.connector_process import spawn_owned, reclaim_orphan
from slack_chat import registry

assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0  # subreaper: this probe waits its orphan
registry.REGISTRY_DIR = os.environ["CONNECTOR_FIXTURE_CLAIMS"]
root = Path(os.environ["CONNECTOR_FIXTURE_DIR"])
key = ThreadKey(sys.argv[1], os.environ["CONNECTOR_FIXTURE_CONVERSATION"], os.environ["CONNECTOR_FIXTURE_ROOT"])
link, session = sys.argv[2:4]
owner = os.fork()
if owner == 0:
    process = spawn_owned(key, [sys.executable, str(root / "bootstrap.py"), key.transport,
                               "-m", key.transport + "_chat.connector", "--permalink", link],
                          os.environ, owner="platform", session=session, permalink=link,
                          log_path=root / "child.log")
    (root / "orphan.pid").write_text(str(process.popen.pid))
    deadline = time.monotonic() + 30
    while not (root / "events.jsonl").read_text() and time.monotonic() < deadline:
        time.sleep(.02)
    os._exit(0)
child = None
try:
    assert os.waitpid(owner, 0)[1] == 0
    child = int((root / "orphan.pid").read_text())
    deadline = time.monotonic() + 30
    while not (root / "events.jsonl").read_text() and time.monotonic() < deadline:
        time.sleep(.02)
    assert (root / "events.jsonl").read_text(), (root / "child.log").read_text()
    for selected_session in (None, "different-session"):
        try: reclaim_orphan(key, session=selected_session, canonical=link)
        except OwnershipBusy: pass
        else: raise AssertionError("unverified supervisor reclaimed a child")
        assert os.waitpid(child, os.WNOHANG) == (0, 0)
    saved = read_json(key.owner_path)
    wrong = dict(saved, child_start="wrong-process-start")
    write_json(key.owner_path, wrong)
    try: reclaim_orphan(key, session=session, canonical=link)
    except OwnershipBusy: pass
    else: raise AssertionError("stale process identity reclaimed a child")
    assert os.waitpid(child, os.WNOHANG) == (0, 0)
    write_json(key.owner_path, saved)
    try:
        reclaim_orphan(key, session=session, canonical=link)
    except OwnershipBusy as exc:
        assert "pidfd signaling unavailable" in str(exc)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        pid, status = os.waitpid(child, os.WNOHANG)
        if pid:
            child = None
            assert os.waitstatus_to_exitcode(status) == 0
            break
        time.sleep(.02)
    assert child is None, "orphan did not drain"
    assert not key.busy()
    print("validated orphan stopped; unrelated/stale selections untouched; every child waited")
finally:
    if child is not None:
        try: os.kill(child, signal.SIGKILL)
        except ProcessLookupError: pass
        os.waitpid(child, 0)
'''


def test_real_orphan_handoff_validates_process_and_supervisor_identity(child):
    import sys
    from lmer_cli.connector_process import child_environment
    from tests.connector_probe_supervisor import run_probe

    child.supervisor.log.append(kind="milestone", text="orphan receipt", source=comms.Source("agent"))
    probe = child.root / "orphan-probe.py"
    probe.write_text(ORPHAN_PROBE)
    result = run_probe([sys.executable, str(probe), child.channel, child.link, child.supervisor.log.session],
                       env=child_environment({**child.environment(), "CONNECTOR_FIXTURE_POLL": "0.5"}),
                       timeout=120, receipt=child.root / "orphan-cleanup.json")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "every child waited" in result.stdout
    assert len(child.events()) == 1


@pytest.mark.parametrize("failure", ["timeout", "nonzero"])
def test_orphan_probe_failure_collects_all_detached_descendants(child, failure):
    import sys
    from lmer_cli.connector_process import child_environment
    from tests.connector_probe_supervisor import run_probe

    bootstrap = child.root / "bootstrap.py"
    bootstrap.write_text(bootstrap.read_text().replace("module = importlib.import_module", "time.sleep(180)\nmodule = importlib.import_module"))
    probe = child.root / "orphan-probe.py"
    source = ORPHAN_PROBE
    if failure == "nonzero":
        source = source.replace("child = None\ntry:", '''
deadline = time.monotonic() + 30
while not (root / "orphan.pid").exists() and time.monotonic() < deadline: time.sleep(.01)
assert (root / "orphan.pid").exists()
sys.exit(9)
child = None
try:''', 1)
    probe.write_text(source)
    receipt = child.root / "orphan-cleanup.json"
    result = run_probe([sys.executable, str(probe), child.channel, child.link, child.supervisor.log.session],
                       env=child_environment(child.environment()), timeout=30, receipt=receipt)
    assert result.returncode == (124 if failure == "timeout" else 9), result.stdout + result.stderr
    saved = json.loads(receipt.read_text())
    assert saved["all_waited"] and len(saved["collected"]) == 2, saved
    assert saved["timed_out"] == (failure == "timeout")
    assert not child.key.busy()
    assert child.events() == []


def test_child_rejects_wrong_owner_session_before_delivery_or_ack(child):
    import hashlib
    import sys
    from lmer_cli.connector_process import spawn_owned

    module = __import__(child.channel + "_chat.connector", fromlist=["SUBSCRIBER"])
    subscriber = module.SUBSCRIBER + ":" + hashlib.sha256(
        f"{child.key.conversation}:{child.key.root}".encode()).hexdigest()[:16]
    child.supervisor.client.ack(subscriber, 22)
    child.supervisor.log.append(kind="milestone", text="wrong owner must not send", source=comms.Source("agent"))
    owned = spawn_owned(child.key, [sys.executable, "-m", child.channel + "_chat.connector", "--permalink", child.link],
                        child.environment(), owner="platform", session="different-session",
                        permalink=child.link, runner=child.runner)
    wait_for(lambda: owned.popen.poll() is not None, child)
    assert owned.collect() == 1
    assert child.events() == []
    assert child.supervisor.client.health()["subscribers"].get(subscriber) == 22
    assert not list(child.key.directory.glob("session-*.json"))


def test_child_imports_installed_connector_despite_project_pythonpath(child, monkeypatch):
    import os

    attack = child.root / "untrusted"
    package = attack / (child.channel + "_chat")
    package.mkdir(parents=True)
    marker = child.root / "wrong-module-ran"
    (package / "__init__.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    (package / "connector.py").write_text("raise RuntimeError('untrusted connector ran')\n")
    monkeypatch.setenv("PYTHONPATH", str(attack))
    monkeypatch.chdir(attack)
    child.supervisor.log.append(kind="milestone", text="trusted module delivered", source=comms.Source("agent"))
    owned = child.spawn()
    try:
        wait_for(lambda: bool(child.events()), child)
        assert not marker.exists()
    finally:
        owned.stop(timeout=30)


def test_module_monitor_stays_live_during_matrix_startup_network_wait(tmp_path, live_supervisor):
    harness = ChildHarness(tmp_path / "joining", "matrix", live_supervisor)
    (harness.root / "mode").write_text("join-block")
    owned = harness.spawn()
    try:
        wait_for(lambda: (harness.root / "join.reached").exists(), harness)
        started = time.monotonic()
        (harness.root / "watched.py").write_text("# changed while join is blocked\n")
        wait_for(lambda: owned.popen.poll() is not None, harness, timeout=60)
        assert time.monotonic() - started < 30
        assert owned.collect() == 3
        assert harness.events() == []
        assert not harness.key.busy()
    finally:
        (harness.root / "mode").write_text("healthy")
        owned.stop(timeout=30)
        harness.close()


@pytest.mark.parametrize("failure", ["metadata", "readiness-pipe"])
def test_failed_parent_handoff_collects_real_child_before_releasing_lease(child, failure, monkeypatch):
    import os
    from lmer_cli import connector_process

    source, _ = child.supervisor.log.append(kind="milestone", text="not authorized to send", source=comms.Source("agent"))
    def fail_metadata(self, pid):
        raise OSError("injected handoff metadata failure")
    real_write = os.write
    def fail_pipe(fd, data):
        if data == b"1":
            raise OSError("injected readiness pipe failure")
        return real_write(fd, data)
    try:
        with monkeypatch.context() as fault:
            if failure == "metadata":
                fault.setattr(Lease, "attach_child", fail_metadata)
            else:
                fault.setattr(connector_process.os, "write", fail_pipe)
            with pytest.raises(OSError, match="injected"):
                child.spawn()
        assert len(child.children) == 1
        assert child.children[0].returncode is not None
        assert child.events() == []
        assert not child.key.busy()
        assert child.supervisor.log.read_since(0)[0][0].id == source.id
    finally:
        child.close()
