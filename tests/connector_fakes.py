"""Real supervisor test rigs with isolated connector persistence."""
import pytest

from lmer_cli import thread_posts
from lmer_platform import store
from slack_chat import registry
from tests.conftest import strip_lmer_env
from tests.timing_budgets import CLI, JOIN, LINK, STOP, child_bounds
from tests.test_matrix_chat import Rig as MatrixRig, ROOM
from tests.test_slack_connector import Rig as SlackRig


@pytest.fixture(autouse=True)
def isolated_connectors(tmp_path, monkeypatch):
    strip_lmer_env(monkeypatch)
    monkeypatch.setattr(thread_posts, "REGISTRY_DIR", str(tmp_path / "posts"))
    monkeypatch.setattr(registry, "REGISTRY_DIR", str(tmp_path / "claims"))
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))


@pytest.fixture
def live_supervisor(tmp_path):
    import socket
    import threading
    import time
    from types import SimpleNamespace

    import uvicorn
    from ask_channel import bus
    from lmer_cli import comms, supervisor

    log = comms.MessageLog("process-session", str(tmp_path / "source.jsonl"))
    app = supervisor._build_fastapi_app(supervisor.OutputBuffer(limit=4096), len, "fixture-control", comms=log)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    endpoint = bus.Endpoint(f"http://127.0.0.1:{listener.getsockname()[1]}", "fixture-control")
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    worker = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + LINK
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        yield SimpleNamespace(log=log, endpoint=endpoint, client=bus.BusClient(endpoint))
    finally:
        server.should_exit = True
        worker.join(timeout=JOIN)
        if worker.is_alive():
            server.force_exit = True
            worker.join(timeout=JOIN)
        listener.close()
        log.close()
        assert not worker.is_alive()


CHILD_SOURCE = r'''
import faulthandler
import fcntl
import hashlib
import importlib
import json
import logging
import os
import signal
from pathlib import Path
import sys
import threading
import time
import uuid

faulthandler.enable()
faulthandler.register(signal.SIGUSR1, all_threads=True)

# Log before imports so an import failure still leaves child evidence.
def say(text):
    print("[fixture-child %d] %s" % (os.getpid(), text), file=sys.stderr, flush=True)

say("start argv=%r" % (sys.argv[1:],))

from lmer_cli import connector_delivery, connector_lifecycle, thread_posts
from slack_chat import registry
from slack_chat.client import SlackError
from matrix_chat.client import MatrixError

root = Path(os.environ["CONNECTOR_FIXTURE_DIR"])
channel = sys.argv[1]
registry.REGISTRY_DIR = os.environ["CONNECTOR_FIXTURE_CLAIMS"]
thread_posts.REGISTRY_DIR = str(root / "posts")
conversation = os.environ["CONNECTOR_FIXTURE_CONVERSATION"]
thread_root = os.environ["CONNECTOR_FIXTURE_ROOT"]

def mode():
    return (root / "mode").read_text().strip()

def current_mode():
    try:
        return mode()
    except OSError as exc:
        return "unreadable (%s)" % exc

def events():
    return [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]

def append(value):
    with (root / "events.jsonl").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())

def matrix_event(value):
    return {"event_id": value["ref"], "sender": value.get("sender", "@lmer:example.org"),
            "type": "m.room.message", "origin_server_ts": value.get("at", 1000),
            "content": {"body": value["text"], "msgtype": value.get("kind", "m.text"),
                        "m.relates_to": {"rel_type": "m.thread", "event_id": thread_root},
                        **value.get("extra", {}), **value.get("file", {})}}

class Remote:
    def auth_test(self): return "UBOT"
    def workspace(self): return {"host": "example.slack.com"}
    def resolve_room(self, room): return conversation
    def join(self, room):
        if mode() == "join-block":
            (root / "join.reached").write_text("reached")
            while mode() == "join-block": time.sleep(.01)
        return conversation
    def room_is_encrypted(self, room): return False
    def get_replies(self, room, thread, oldest=None):
        if mode() == "inbound-fail": raise RuntimeError("injected persistent inbound failure")
        result = [{"ts": thread_root, "text": "root", "user": "U1"}]
        result += [{"ts": item["ref"], "thread_ts": thread_root, "text": item["text"],
                    "user": item.get("sender", "UBOT")} for item in events()
                   if oldest is None or item["ref"] > oldest]
        return result
    def _request(self, method, endpoint, **kwargs):
        return {"ok": True, "user": {"profile": {"display_name": "operator"}}}
    def get_event(self, room, ref):
        if ref == thread_root:
            return {"event_id": ref, "sender": "@operator:example.org", "type": "m.room.message",
                    "origin_server_ts": 1, "content": {"body": "root", "msgtype": "m.text"}}
        return matrix_event(next(item for item in events() if item["ref"] == ref))
    def thread_events(self, room, root, limit=None): return [matrix_event(item) for item in events()]
    def sync(self, *, since=None, **kwargs):
        if mode() == "inbound-fail": raise RuntimeError("injected persistent inbound failure")
        time.sleep(0.02)
        timeline = events()
        return {"next_batch": str(len(timeline)), "rooms": {"join": {conversation: {
            "timeline": {"events": [matrix_event(item) for item in timeline[int(since or 0):]]}}}}}
    def send(self, text, kind="m.text", extra=None, file=None):
        current = mode()
        once = root / (current + ".reached")
        if current == "rejection" and not once.exists():
            once.write_text("reached")
            if channel == "slack": raise SlackError("limited", code="ratelimited")
            raise MatrixError("limited", status=429, errcode="M_LIMIT_EXCEEDED")
        ref = f"{time.time():.6f}" if channel == "slack" else "$" + uuid.uuid4().hex
        append({"ref": ref, "text": text, "kind": kind, "extra": extra or {}, "file": file or {}})
        if current == "wait-after-send":
            once.write_text("reached")
            while mode() == current: time.sleep(0.01)
        if (current == "unknown" and not once.exists() or current == "unknown-every-send"
            or current == "second-file-unknown" and kind == "m.file" and len([x for x in events() if x["kind"] == "m.file"]) == 2):
            once.write_text("reached")
            raise RuntimeError("remote accepted but its response was lost")
        return ref
    def post_message(self, room, thread, text, **kwargs): return self.send(text)
    def send_thread_text(self, room, thread, text, *, msgtype="m.text", extra=None, **kwargs):
        return self.send(text, msgtype, extra)
    def upload(self, data, **kwargs):
        name = hashlib.sha256(data).hexdigest()
        (root / name).write_bytes(data)
        return "mxc://fixture/" + name
    def download(self, uri, *, max_bytes=None):
        data = (root / uri.rsplit("/", 1)[-1]).read_bytes()
        return (None if max_bytes is not None and len(data) > max_bytes else data), "text/plain"
    def send_thread_file(self, room, thread, *, mxc, name, media_type, size, extra=None, **kwargs):
        return self.send(name, "m.file", extra, {"url": mxc, "filename": name, "info": {"size": size, "mimetype": media_type}})

remote = Remote()
if sys.argv[2] == "recover":
    from lmer_cli import connector_recovery, thread_access
    thread_access._slack_client = lambda *args: remote
    thread_access._matrix_client = lambda *args: remote
    original_write = connector_recovery.write_json
    def interrupt_recovery(path, value):
        original_write(path, value)
        if mode() == "interrupt-recovery" and not (root / "recovery.reached").exists():
            (root / "recovery.reached").write_text("reached")
            os._exit(71)
    connector_recovery.write_json = interrupt_recovery
    recovery_code = connector_recovery.main(sys.argv[3:])
    say("recovery exit code=%r" % (recovery_code,))
    sys.exit(recovery_code)

# Leave the recovery CLI's logging configuration intact.
logging.basicConfig(level=logging.INFO, stream=sys.stderr, force=True,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

module = importlib.import_module(channel + "_chat.connector")
module.__file__ = str(root / "watched.py")
connector_lifecycle.ConnectorLifecycle._bus_poll_seconds = 0.02
getattr(module, "SlackConnector" if channel == "slack" else "MatrixConnector")._bus_poll_seconds = 0.02
if channel == "slack": module.SlackClient = lambda *args, **kwargs: remote
else: module.MatrixClient = lambda *args, **kwargs: remote
connector_lifecycle.DRAIN_SECONDS = float(os.environ["CONNECTOR_FIXTURE_DRAIN"])
connector_lifecycle.MODULE_POLL_SECONDS = float(os.environ["CONNECTOR_FIXTURE_POLL"])
connector_lifecycle.FAILURE_LIMIT = 2
connector_lifecycle.ConnectorLifecycle._backoff = lambda self: self._stop.wait(0.02)
original_write = connector_delivery.write_json

def fail_receipt(path, value):
    pending = value.get("pending") or {}
    if mode() == "receipt-failure" and any(part.get("receipt") for part in pending.get("parts", {}).values()):
        (root / "receipt.reached").write_text("reached")
        raise OSError("injected confirmed receipt persistence failure")
    return original_write(path, value)
connector_delivery.write_json = fail_receipt
original_clear = connector_delivery.DeliveryState.clear_marker
def fail_marker_clear(self, operation):
    if mode() == "receipt-saved":
        raise OSError("injected marker removal failure after saved receipt")
    return original_clear(self, operation)
connector_delivery.DeliveryState.clear_marker = fail_marker_clear
real_exec = os.execv
original_args = list(sys.argv)
def reexec(executable, argv):
    with (root / "execs").open("a") as stream: stream.write(str(os.getpid()) + "\n")
    say("reload exec")
    real_exec(executable, [executable, *original_args])
os.execv = reexec
args = sys.argv[2:]
if args[:1] == ["-m"]: args = args[2:]
sys.argv = [module.__name__, *args]
code = None
try:
    code = module.main(args)
except BaseException as exc:
    say("raised %s: %s" % (type(exc).__name__, exc))
    raise
finally:
    say("exit code=%r mode=%s" % (code, current_mode()))
sys.exit(code)
'''


class ChildHarness:
    def __init__(self, root, channel, supervisor):
        import json
        from pathlib import Path
        from lmer_cli.connector_ownership import ThreadKey

        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.channel, self.supervisor = channel, supervisor
        self.key = ThreadKey(channel, "C1" if channel == "slack" else ROOM,
                             "1699999999.000000" if channel == "slack" else "$root_event_id")
        self.link = ("https://example.slack.com/archives/C1/p1699999999000000" if channel == "slack"
                     else "https://matrix.to/#/!room:example.org/$root_event_id")
        (self.root / "bootstrap.py").write_text(CHILD_SOURCE)
        (self.root / "watched.py").write_text("# watched connector module\n")
        (self.root / "events.jsonl").write_text("")
        (self.root / "mode").write_text("healthy")
        self.children = []
        self.log_paths = []
        self.bounds = child_bounds()

    def environment(self):
        import os
        return {**os.environ, "CONNECTOR_FIXTURE_DIR": str(self.root),
                "CONNECTOR_FIXTURE_CLAIMS": str(registry.REGISTRY_DIR),
                "CONNECTOR_FIXTURE_CONVERSATION": self.key.conversation,
                "CONNECTOR_FIXTURE_ROOT": self.key.root,
                "LMER_SUPERVISOR_URL": self.supervisor.endpoint.url,
                "LMER_FASTAPI_TOKEN": self.supervisor.endpoint.token,
                "SLACK_BOT_TOKEN": "fixture-bot", "LMER_CHAT_ATTACH_ACK": "0",
                "LMER_SLACK_POLL_SECONDS": "0.02",
                "LMER_MATRIX_HOMESERVER": "https://fixture.invalid",
                "LMER_MATRIX_USER_ID": "@lmer:example.org", "LMER_MATRIX_ACCESS_TOKEN": "fixture-token",
                "LMER_MATRIX_AUTHENTICATED_MEDIA": "1",
                # Survives reload execv; a -u flag is lost when argv is rebuilt.
                "PYTHONUNBUFFERED": "1", **self.bounds}

    def runner(self, argv, env, log_path=None, *, pass_fds=()):
        import sys
        from lmer_cli.connector_process import spawn_child

        env = {**env, **{key: value for key, value in self.environment().items()
                        if key.startswith("CONNECTOR_FIXTURE_")}}
        log_path = log_path or self.root / "child.log"
        self.log_paths.append(log_path)
        process = spawn_child([sys.executable, str(self.root / "bootstrap.py"), self.channel, *argv[1:]],
                              env, log_path, pass_fds=pass_fds)
        self.children.append(process)
        return process

    def spawn(self, *, owner="direct"):
        import sys
        from lmer_cli.connector_process import spawn_owned

        return spawn_owned(self.key, [sys.executable, "-m", self.channel + "_chat.connector",
                                     "--permalink", self.link, "--uploads-dir", str(self.root / "uploads"),
                                     "--container-uploads-dir", "/uploads"], self.environment(), owner=owner,
                           session=self.supervisor.log.session, permalink=self.link, runner=self.runner,
                           log_path=self.root / "child.log")

    def cli(self, *args):
        import subprocess
        import sys
        from lmer_cli.connector_process import child_environment

        return subprocess.run([sys.executable, str(self.root / "bootstrap.py"), self.channel, "recover",
                               "--permalink", self.link, *args], env=child_environment(self.environment()),
                              capture_output=True, text=True, timeout=CLI)

    def events(self):
        import json
        return [json.loads(line) for line in (self.root / "events.jsonl").read_text().splitlines()]

    def close(self):
        from lmer_cli.connector_process import terminate_wait
        for process in self.children:
            terminate_wait(process, timeout=STOP)
