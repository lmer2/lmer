"""Actual process regressions for delivery and shutdown boundaries."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from lmer_cli import comms
from lmer_cli.connector_delivery import checkpoint_path, marker_path
from lmer_cli.connector_ownership import read_json
from tests.connector_fakes import CHILD_SOURCE, isolated_connectors, live_supervisor
from tests.test_connector_process import child, wait_for
from tests.test_connector_recovery import block


def instrument(child, code):
    path = child.root / "bootstrap.py"
    path.write_text(path.read_text().replace("args = sys.argv[2:]", code + "\nargs = sys.argv[2:]"))


def test_sigterm_during_event_wait_lock_completes_shutdown(child):
    instrument(child, '''
import signal
import threading
original_run = connector_lifecycle.ConnectorLifecycle.run_forever
def run(self):
    original_wait = self._stop.wait
    def wait(timeout=None):
        if (threading.current_thread() is threading.main_thread()
                and (root / "signal-now").exists() and not (root / "signal-entered").exists()):
            with self._stop._cond:
                (root / "signal-entered").touch()
                os.kill(os.getpid(), signal.SIGTERM)
                (root / "signal-returned").touch()
        return original_wait(timeout)
    self._stop.wait = wait
    return original_run(self)
connector_lifecycle.ConnectorLifecycle.run_forever = run
''')
    source, _ = child.supervisor.log.append(kind="milestone", text="confirmed before signal", source=comms.Source("agent"))
    owned = child.spawn()
    try:
        wait_for(lambda: bool(child.events()) and (read_json(checkpoint_path(child.key, child.supervisor.log.session)) or {}).get("pending") is None, child)
        (child.root / "signal-now").touch()
        wait_for(lambda: (child.root / "signal-returned").exists(), child, timeout=60)
        wait_for(lambda: owned.popen.poll() is not None, child, timeout=60)
        assert owned.collect() == 0
        assert not child.key.busy()
        assert len(child.events()) == 1
        assert child.supervisor.log.read_since(0)[0][0].id == source.id
    finally:
        owned.stop(timeout=30)


def test_sigterm_during_reload_finalization_exits_without_exec(child):
    instrument(child, '''
connector_lifecycle.DRAIN_SECONDS = 60
original_close = connector_delivery.DeliveryState.close
def close(self):
    if (root / "pause-finalize").exists():
        (root / "finalize-entered").touch()
        deadline = time.monotonic() + 30
        while not (root / "resume-finalize").exists():
            if time.monotonic() >= deadline: raise RuntimeError("finalization fixture was not released")
            time.sleep(.01)
    return original_close(self)
connector_delivery.DeliveryState.close = close
''')
    source, _ = child.supervisor.log.append(kind="milestone", text="confirmed before reload", source=comms.Source("agent"))
    owned = child.spawn()
    try:
        wait_for(lambda: bool(child.events()) and (read_json(checkpoint_path(child.key, child.supervisor.log.session)) or {}).get("pending") is None, child)
        (child.root / "pause-finalize").touch()
        (child.root / "watched.py").write_text("# request reload\n")
        wait_for(lambda: (child.root / "finalize-entered").exists(), child, timeout=60)
        owned.popen.terminate()
        (child.root / "resume-finalize").touch()
        wait_for(lambda: owned.popen.poll() is not None or (child.root / "execs").exists(), child, timeout=60)
        assert not (child.root / "execs").exists(), "SIGTERM was followed by reload exec"
        assert owned.collect() == 0
        assert not child.key.busy()
        assert len(child.events()) == 1
        assert child.supervisor.log.read_since(0)[0][0].id == source.id
    finally:
        (child.root / "resume-finalize").touch()
        owned.stop(timeout=30)


def test_native_module_reload_preserves_entrypoint_and_lease(child):
    from lmer_cli.connector_process import spawn_owned

    package = child.channel + "_chat"
    code = child.root / "native"
    code.mkdir()
    shutil.copytree(Path(__file__).resolve().parents[1] / "src" / package, code / package)
    prefix = CHILD_SOURCE.split("remote = Remote()", 1)[0].replace(
        "channel = sys.argv[1]", f"channel = {child.channel!r}")
    (code / "sitecustomize.py").write_text(prefix + '''
from ask_channel import bus
from slack_chat import client as slack_client
from matrix_chat import client as matrix_client
remote = Remote()
slack_client.SlackClient = lambda *args, **kwargs: remote
matrix_client.MatrixClient = lambda *args, **kwargs: remote
assert connector_lifecycle.MODULE_POLL_SECONDS == 1
assert connector_lifecycle.DRAIN_SECONDS == 45
connector_lifecycle.DRAIN_SECONDS = 30
original_messages = bus.BusClient.messages
def messages(self, cursor, **kwargs):
    return original_messages(self, cursor, timeout=.02)
bus.BusClient.messages = messages
original_ready = connector_lifecycle.ConnectorLifecycle._run_when_ready
def ready(self):
    original_ready(self)
    with (root / "boots").open("a") as stream:
        stream.write(json.dumps({"pid": os.getpid(), "module": type(self).__module__}) + "\\n")
connector_lifecycle.ConnectorLifecycle._run_when_ready = ready
original_stop = connector_lifecycle.ConnectorLifecycle.stop
def stop(self, *args, **kwargs):
    (root / "stop-start").write_text(str(time.monotonic()))
    return original_stop(self, *args, **kwargs)
connector_lifecycle.ConnectorLifecycle.stop = stop
def audit(event, args):
    if event == "os.exec":
        with (root / "exec-argv").open("a") as stream:
            stream.write(json.dumps(args[1]) + "\\n")
sys.addaudithook(audit)
''')
    def runner(argv, env, log_path=None, *, pass_fds=()):
        env = {**env, "PYTHONPATH": str(code) + os.pathsep + str(Path(__file__).resolve().parents[1] / "src")}
        log_path = child.root / "native.log"
        child.log_paths.append(log_path)
        with log_path.open("ab") as stream:
            process = subprocess.Popen(argv, env=env, stdout=stream, stderr=stream,
                                       pass_fds=pass_fds, start_new_session=True)
        child.children.append(process)
        return process
    child.supervisor.log.append(kind="milestone", text="native before", source=comms.Source("agent"))
    owned = spawn_owned(child.key, [sys.executable, "-m", package + ".connector", "--permalink", child.link],
                        child.environment(), owner="direct", session=child.supervisor.log.session, runner=runner)
    try:
        wait_for(lambda: (code.parent / "boots").exists() and bool(child.events()), child)
        token = read_json(child.key.owner_path)["token"]
        module = code / package / "connector.py"
        replacement = module.with_suffix(".replacement")
        replacement.write_text(module.read_text() + "\n# module replacement\n")
        import time
        changed_at = time.monotonic()
        replacement.replace(module)
        wait_for(lambda: owned.popen.poll() is not None or len((child.root / "boots").read_text().splitlines()) == 2, child)
        argv = json.loads((child.root / "exec-argv").read_text())
        assert argv[1:3] == ["-m", package + ".connector"], (argv, (child.root / "native.log").read_text())
        assert owned.popen.poll() is None, (child.root / "native.log").read_text()
        assert float((child.root / "stop-start").read_text()) - changed_at < 10
        boots = [json.loads(line) for line in (child.root / "boots").read_text().splitlines()]
        assert boots == [{"pid": owned.popen.pid, "module": "__main__"}] * 2
        assert read_json(child.key.owner_path)["token"] == token
        child.supervisor.log.append(kind="milestone", text="native after", source=comms.Source("agent"))
        wait_for(lambda: len(child.events()) == 2, child)
        assert sum("native before" in item["text"] for item in child.events()) == 1
    finally:
        owned.stop(timeout=30)


@pytest.mark.parametrize("stage", ["checkpoint", "final-ack", "lease", "lease-close"])
def test_finalization_is_inside_total_reload_deadline(child, stage):
    instrument(child, '''
from ask_channel import bus
from lmer_cli.connector_ownership import Lease
stage = ''' + repr(stage) + '''
def delay():
    if (root / "finalize").exists():
        (root / "finalize.reached").write_text(stage)
        time.sleep(60)
if stage == "checkpoint":
    original = connector_delivery.DeliveryState.close
    def delayed(self):
        delay()
        return original(self)
    connector_delivery.DeliveryState.close = delayed
elif stage == "final-ack":
    original = bus.BusClient.ack
    def delayed(self, *args, **kwargs):
        if kwargs.get("final"): delay()
        return original(self, *args, **kwargs)
    bus.BusClient.ack = delayed
elif stage == "lease":
    original = Lease.attach_child
    def delayed(self, *args):
        delay()
        return original(self, *args)
    Lease.attach_child = delayed
else:
    original = Lease.close
    def delayed(self, *args, **kwargs):
        delay()
        result = original(self, *args, **kwargs)
        (root / "lease-closed").touch()
        return result
    Lease.close = delayed
original_stop = connector_lifecycle.ConnectorLifecycle.stop
def stop(self, *args, **kwargs):
    started = time.monotonic()
    result = original_stop(self, *args, **kwargs)
    (root / "stop-result").write_text(json.dumps({"ok": result, "elapsed": time.monotonic()-started}))
    if not result:
        while not (root / "exit").exists(): time.sleep(.01)
    return result
connector_lifecycle.ConnectorLifecycle.stop = stop
''')
    source, _ = child.supervisor.log.append(kind="milestone", text="keep confirmed delivery", source=comms.Source("agent"))
    owned = child.spawn()
    try:
        wait_for(lambda: bool(child.events()) and (read_json(checkpoint_path(child.key, child.supervisor.log.session)) or {}).get("pending") is None, child)
        owned.lease.close(release=False)
        (child.root / "finalize").touch()
        if stage == "lease-close":
            owned.popen.terminate()
        else:
            (child.root / "watched.py").write_text("# finalize under total deadline\n")
        wait_for(lambda: (child.root / "stop-result").exists() and (child.root / "finalize.reached").exists(), child, timeout=60)
        result = json.loads((child.root / "stop-result").read_text())
        assert not result["ok"], result
        assert result["elapsed"] < 30, result
        assert (child.root / "finalize.reached").read_text() == stage
        if stage == "lease-close":
            wait_for(lambda: (child.root / "lease-closed").exists(), child, timeout=120)
        assert child.key.busy()
        assert not (child.root / "execs").exists()
        assert len(child.events()) == 1
        assert child.supervisor.log.read_since(0)[0][0].id == source.id
        (child.root / "exit").touch()
        wait_for(lambda: owned.popen.poll() is not None, child, timeout=60)
        assert owned.collect() == 3
        assert not child.key.busy()
    finally:
        (child.root / "exit").touch()
        owned.stop(timeout=30)


@pytest.mark.parametrize("boundary", ["open", "published"])
def test_interrupted_marker_publication_has_supported_recovery(child, boundary):
    instrument(child, '''
boundary = ''' + repr(boundary) + '''
original_open = os.open
def opened(path, flags, *args, **kwargs):
    fd = original_open(path, flags, *args, **kwargs)
    if boundary == "open" and Path(path).name.startswith("pending-send.json") and flags & os.O_EXCL:
        os._exit(71)
    return fd
os.open = opened
original_sync = connector_delivery.sync_directory
def synced(path):
    original_sync(path)
    if boundary == "published" and (Path(path) / "pending-send.json").exists(): os._exit(71)
connector_delivery.sync_directory = synced
''')
    source, _ = child.supervisor.log.append(kind="milestone", text="publication boundary", source=comms.Source("agent"))
    owned = child.spawn()
    wait_for(lambda: owned.popen.poll() is not None, child)
    assert owned.collect() == 71
    assert child.events() == []
    path = marker_path(child.key)
    if path.exists():
        result = child.cli("inspect")
        assert result.returncode == 0, result.stderr
        inspection = json.loads(result.stdout)
        result = child.cli("retry", "--operation", inspection["operation"], "--inspected-thread", "--accept-possible-duplicate")
        assert result.returncode == 0, result.stderr
    (child.root / "bootstrap.py").write_text(CHILD_SOURCE)
    resumed = child.spawn()
    try:
        wait_for(lambda: len(child.events()) == 1 and read_json(checkpoint_path(child.key, child.supervisor.log.session))["pending"] is None, child)
        assert child.supervisor.log.read_since(0)[0][0].id == source.id
    finally:
        resumed.stop(timeout=30)


def test_interrupted_retry_then_receipt_resumes_exact_selection(child):
    source, inspection = block(child)
    op = inspection["operation"]
    (child.root / "mode").write_text("interrupt-recovery")
    result = child.cli("retry", "--operation", op, "--inspected-thread", "--accept-possible-duplicate")
    assert result.returncode == 71, result.stderr
    retry = read_json(checkpoint_path(child.key, child.supervisor.log.session))["pending"]
    unused_attempt = next(iter(retry["parts"].values()))["id"]
    assert unused_attempt != op
    (child.root / "recovery.reached").unlink()
    result = child.cli("receipt", "--operation", op, "--remote-ref", child.events()[0]["ref"])
    assert result.returncode == 71, result.stderr
    before = checkpoint_path(child.key, child.supervisor.log.session).read_bytes()
    pending = json.loads(before)["pending"]
    assert pending["source"]["id"] == source.id
    for wrong in ("wrong", unused_attempt):
        result = child.cli("receipt", "--operation", wrong)
        assert result.returncode == 2, result.stdout
        assert checkpoint_path(child.key, child.supervisor.log.session).read_bytes() == before
    result = child.cli("inspect")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["operation"] == op
    assert child.cli("retry", "--operation", op, "--inspected-thread", "--accept-possible-duplicate").returncode == 2
    result = child.cli("receipt", "--operation", op)
    assert result.returncode == 0, result.stderr
    assert child.cli("receipt", "--operation", op).returncode == 2
    (child.root / "mode").write_text("healthy")
    resumed = child.spawn()
    try:
        wait_for(lambda: read_json(checkpoint_path(child.key, child.supervisor.log.session))["pending"] is None, child)
        assert len(child.events()) == 1
        assert child.supervisor.log.read_since(0)[0][0].id == source.id
    finally:
        resumed.stop(timeout=30)


def test_completed_receipt_identity_outlives_echo_eviction(child):
    for _ in range(257):
        child.supervisor.log.append(kind="milestone", text="identical content", source=comms.Source("agent"))
    first = child.spawn()
    try:
        wait_for(lambda: len(child.events()) == 257 and read_json(checkpoint_path(child.key, child.supervisor.log.session))["pending"] is None, child, timeout=900)
    finally:
        first.stop(timeout=30)
    old = child.events()[0]["ref"]
    saved = read_json(checkpoint_path(child.key, child.supervisor.log.session))
    assert old not in saved["transport"]["posters"]["wrote"]
    (child.root / "mode").write_text("unknown")
    source, _ = child.supervisor.log.append(kind="milestone", text="identical content", source=comms.Source("agent"))
    second = child.spawn()
    wait_for(lambda: second.popen.poll() is not None, child)
    assert second.collect() == 4
    op = json.loads(child.cli("inspect").stdout)["operation"]
    before = checkpoint_path(child.key, child.supervisor.log.session).read_bytes()
    result = child.cli("receipt", "--operation", op, "--remote-ref", old)
    assert result.returncode == 2, "old evicted receipt falsely confirmed current source: " + result.stdout
    assert checkpoint_path(child.key, child.supervisor.log.session).read_bytes() == before
    assert marker_path(child.key).exists()
    result = child.cli("receipt", "--operation", op, "--remote-ref", child.events()[-1]["ref"])
    assert result.returncode == 0, result.stderr
    (child.root / "mode").write_text("healthy")
    resumed = child.spawn()
    try:
        wait_for(lambda: read_json(checkpoint_path(child.key, child.supervisor.log.session))["pending"] is None, child)
        assert len(child.events()) == 258
        assert any(item.id == source.id for item in child.supervisor.log.read_since(0)[0])
    finally:
        resumed.stop(timeout=30)
