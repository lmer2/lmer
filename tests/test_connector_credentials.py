"""Credential identity across the shared platform connector's actual child launch."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from lmer_cli import connector_process
from lmer_platform import config, connectors, session_io, store
from tests.connector_fakes import isolated_connectors  # noqa: F401
from tests.test_platform_thread_connectors import (
    CONTROL_TOKEN, FakeMatrix, FakeSlack, MATRIX_LINK, SLACK_LINK, live_session,
)


@pytest.fixture
def credential_child(tmp_path, monkeypatch):
    daemon_cwd, child_cwd, home = (tmp_path / name for name in ("daemon", "child", "home"))
    for path in (daemon_cwd, child_cwd, home):
        path.mkdir()
    monkeypatch.chdir(daemon_cwd)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(store, "PLATFORM_DIR", str(home / ".lmer" / "platform"))
    interpreter = child_cwd / "python"
    interpreter.symlink_to(sys.executable)
    # Control the launcher's cwd directory while retaining the active venv in argv.
    monkeypatch.setattr(connector_process, "sys", SimpleNamespace(executable=str(interpreter)))
    monkeypatch.setattr(session_io, "quiet_health", lambda _session: {"session": "fixture-supervisor"})
    monkeypatch.setenv(config.ENV_CONTAINER_URL, "https://container.example.test:9443")
    monkeypatch.setenv("LMER_PLATFORM_URL", "https://container.example.test:9554")
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            authenticated = self.headers.get("Authorization") == "Bearer fixture-daemon-credential"
            observed.append({"transport": body["transport"], "authenticated": authenticated,
                             "path": self.path})
            self.send_response(200 if authenticated else 401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"accepted"}')

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(daemon_cwd=daemon_cwd, child_cwd=child_cwd, home=home,
                              interpreter=interpreter, port=server.server_port, observed=observed)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize("transport", ["slack", "matrix"])
@pytest.mark.parametrize("setting", [
    "relative-only", "relative-decoy", "absolute", "home", "default", "symlink-parent",
    "missing", "empty", "unreadable-directory", "symlink-loop",
])
def test_credential_identity_through_owned_connector_child(credential_child, transport, setting):
    rig = credential_child
    intended = rig.daemon_cwd / "credential"
    configured = "credential"
    if setting == "absolute":
        configured = str(intended)
    elif setting == "home":
        intended = rig.home / "credential"
        configured = "~/credential"
    elif setting == "default":
        intended = config.PlatformConfig().secret_path
        configured = None
    elif setting == "symlink-parent":
        target = rig.daemon_cwd / "target"
        (target / "nested").mkdir(parents=True)
        (rig.daemon_cwd / "alias").symlink_to(target / "nested", target_is_directory=True)
        intended = target / "credential"
        configured = "alias/../credential"
        (rig.daemon_cwd / "credential").write_text("fixture-parent-decoy\n")
        (rig.daemon_cwd / "credential").chmod(0o600)

    intended.parent.mkdir(parents=True, exist_ok=True)
    if setting == "unreadable-directory":
        intended.mkdir()
    elif setting == "symlink-loop":
        intended.symlink_to(intended.name)
    elif setting != "missing":
        intended.write_text("\n  \n" if setting == "empty" else "fixture-daemon-credential\n")
        intended.chmod(0o600)
    if setting in {"relative-decoy", "missing", "empty", "unreadable-directory", "symlink-loop"}:
        (rig.child_cwd / "credential").write_text("fixture-child-decoy\n")
        (rig.child_cwd / "credential").chmod(0o600)
    if setting != "default":
        fallback = config.PlatformConfig().secret_path
        fallback.parent.mkdir(parents=True, exist_ok=True)
        fallback.write_text("fixture-default-decoy\n")
        fallback.chmod(0o600)

    resolved = config.load({"secret_file": configured, "bind_port": rig.port})
    unreadable = setting in {"unreadable-directory", "symlink-loop"}
    if unreadable:
        with pytest.raises(config.ConfigError, match="cannot read secret file"):
            config.read_secret(resolved)
    else:
        assert bool(config.read_secret(resolved)) == (setting not in {"missing", "empty"})
    children = []
    logs = []
    probe = Path(__file__).parent / "data" / "connector_credentials" / "child.py"

    def runner(argv, env, log_path=None, *, pass_fds=()):
        assert argv[1:3] == ["-m", f"{transport}_chat.connector"]
        key = ("C0123ABCDEF", "1712345678.000900") if transport == "slack" else ("!room:example.org", "$event")
        process = connectors._spawn_process(
            [sys.executable, str(probe), transport, *key], env, log_path, pass_fds=pass_fds,
        )
        children.append(process)
        logs.append(log_path)
        return process

    environment = {**os.environ, "SLACK_BOT_TOKEN": "fixture-bot", "LMER_CHAT_ATTACH_ACK": "0",
                   "LMER_MATRIX_HOMESERVER": "https://matrix.example.test",
                   "LMER_MATRIX_USER_ID": "@fixture:example.test",
                   "LMER_MATRIX_ACCESS_TOKEN": "fixture-matrix"}
    manager = connectors.ThreadConnectors(
        runner=runner, environ=environment, platform_config=resolved,
        slack_client=lambda *_args: FakeSlack(), matrix_client=lambda *_args: FakeMatrix(),
    )
    session = live_session("credential-worker")
    try:
        manager.connect(session, SLACK_LINK if transport == "slack" else MATRIX_LINK, by="test operator")
        assert len(children) == 1, manager.listing(session)
        assert children[0].wait(timeout=15) == 0, logs[0].read_text()
        result = json.loads(logs[0].read_text())
    finally:
        for process in children:
            connector_process.terminate_wait(process, timeout=5)
        manager.stop()

    assert result["cwd"] == str(rig.child_cwd)
    assert Path(result["config_module"]).resolve() == Path(config.__file__).resolve()
    assert result["port"] == rig.port
    assert result["container_url"] == environment[config.ENV_CONTAINER_URL]
    assert result["platform_url"] == environment["LMER_PLATFORM_URL"]
    assert result["supervisor_url"] == "http://127.0.0.1:8123"
    assert Path(result["path"]).is_absolute()
    if unreadable:
        assert "cannot read secret file" in result["read_error"]
        assert result["forward_error"] == "ConfigError"
        assert not rig.observed
    elif setting in {"missing", "empty"}:
        assert not result["found"]
        assert result["forward_error"] == "PlatformError"
        assert not rig.observed
    else:
        info = intended.stat()
        assert result["identity"] == [info.st_dev, info.st_ino]
        assert result["found"]
        assert result["forward"] == {"status": "accepted"}
        assert rig.observed == [{"transport": transport, "authenticated": True,
                                 "path": "/api/assistant/requests"}]
    assert CONTROL_TOKEN not in logs[0].read_text()
    assert "fixture-daemon-credential" not in logs[0].read_text()
    assert "fixture-child-decoy" not in logs[0].read_text()
