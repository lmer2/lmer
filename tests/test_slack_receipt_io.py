"""Receipt HTTP deadlines cover stalled responses and connector draining."""
import json
import os
import shutil
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from lmer_cli import connector_ownership, connector_receipts
from slack_chat import client as slack_client
from tests.connector_fakes import SlackRig, isolated_connectors


@pytest.fixture
def reaction_http(monkeypatch, request, tmp_path):
    state = SimpleNamespace(mode="success", reached=threading.Event(),
                            release=threading.Event(), calls=[],
                            production_budget=slack_client.REACTION_BUDGET_SECONDS)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.calls.append((self.path, self.headers.get("Authorization"), payload))
            state.reached.set()
            if state.mode == "headers":
                state.release.wait(15)
                return
            status = 429 if state.mode == "limited" else 200
            data = {"ok": True} if state.mode == "success" else {"ok": False, "error": state.mode}
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "86400")
            self.send_header("Content-Length", "10000" if state.mode == "body" else str(len(body)))
            self.end_headers()
            try:
                if state.mode == "body":
                    while not state.release.wait(.02):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    scheme = "http"
    if getattr(request, "param", None) == "https":
        state.ca_file = tmp_path / "ca.pem"
        key_file = tmp_path / "key.pem"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-days", "1", "-subj", "/CN=localhost",
            "-addext", "subjectAltName=IP:127.0.0.1",
            "-keyout", str(key_file), "-out", str(state.ca_file),
        ], check=True, capture_output=True, timeout=15)
        state.ca_dir = tmp_path / "certs"
        state.ca_dir.mkdir()
        shutil.copyfile(state.ca_file, state.ca_dir / "ca.pem")
        subprocess.run(["openssl", "rehash", str(state.ca_dir)],
                       check=True, capture_output=True, timeout=15)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(state.ca_file, key_file)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    client = slack_client.SlackClient("fixture-bot")
    client.BASE_URL = f"{scheme}://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(slack_client, "REACTION_BUDGET_SECONDS", 2)
    try:
        yield client, state
    finally:
        state.release.set()
        client.session.close()
        server.shutdown()
        server.server_close()
        worker.join(5)
        assert not worker.is_alive()


@pytest.mark.parametrize("mode", ["success", "already_reacted", "missing_scope"])
def test_reaction_request_and_duplicate_response(reaction_http, mode):
    client, state = reaction_http
    state.mode = mode
    if mode == "missing_scope":
        with pytest.raises(slack_client.SlackError) as error:
            client.add_reaction("C1", "1.23", "inbox_tray")
        assert error.value.code == "missing_scope"
    else:
        client.add_reaction("C1", "1.23", "inbox_tray")
    assert state.calls == [("/reactions.add", "Bearer fixture-bot", {
        "channel": "C1", "timestamp": "1.23", "name": "inbox_tray"})]


@pytest.mark.parametrize("mode", ["headers", "body", "limited"])
def test_reaction_budget_covers_headers_dripping_body_and_rate_limits(reaction_http, mode):
    client, state = reaction_http
    state.mode = mode
    started = time.monotonic()
    with pytest.raises(slack_client.SlackError, match="Reaction request failed"):
        client.add_reaction("C1", "1.23", "inbox_tray")
    assert time.monotonic() - started < 8
    assert len(state.calls) == 1


@pytest.mark.parametrize("reload", [False, True])
@pytest.mark.parametrize("stall", ["headers", "dns", "cleanup"])
def test_stalled_receipt_does_not_prevent_shutdown_or_reload(
        reaction_http, tmp_path, monkeypatch, reload, stall):
    client, state = reaction_http
    state.mode = "headers" if stall == "headers" else "success"
    monkeypatch.setattr(slack_client, "REACTION_BUDGET_SECONDS", state.production_budget)
    child = None
    if stall != "headers":
        probe = tmp_path / "stalled_worker.py"
        marker = tmp_path / "blocked"
        probe.write_text(
            "import atexit, os, runpy, socket, time\n"
            "from pathlib import Path\n"
            "def block(*args, **kwargs):\n"
            f"    Path({str(marker)!r}).write_text(str(os.getpid()))\n"
            "    time.sleep(60)\n"
            + ("socket.getaddrinfo = block\n" if stall == "dns" else "atexit.register(block)\n")
            + f"runpy.run_path({str(slack_client._REACTION_WORKER)!r}, run_name='__main__')\n"
        )
        monkeypatch.setattr(slack_client, "_REACTION_WORKER", probe)
    rig = SlackRig(tmp_path)
    connector = rig.connector
    monkeypatch.setattr(rig.slack, "add_reaction", client.add_reaction)
    monkeypatch.setattr(connector_receipts, "GRACE_SECONDS", 0)
    monkeypatch.setattr(connector_receipts, "POLL_SECONDS", .01)
    for name in (connector_ownership.LEASE_FD_ENV, connector_ownership.LEASE_TOKEN_ENV,
                 connector_ownership.LEASE_DIRECTORY_ENV):
        monkeypatch.setenv(name, os.environ.get(name, ""))
        monkeypatch.delenv(name)
    try:
        connector._inbound_once()
        rig.slack.human_says("while the model is busy")
        connector._inbound_once()
        worker = threading.Thread(target=connector._receipts.run, daemon=True)
        connector._threads.append(worker)
        worker.start()
        if stall == "headers":
            assert state.reached.wait(10)
        else:
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert marker.exists(), "The child did not reach the stalled operation"
            child = int(marker.read_text())
            assert (len(state.calls) == 1) == (stall == "cleanup")
        started = time.monotonic()
        assert connector.stop(announce=False, release=not reload)
        assert time.monotonic() - started < 10
        assert not worker.is_alive()
        assert connector.exit_code == 0
        assert not connector._receipt_pending
        if reload:
            assert not connector._lease.closed
            assert os.get_inheritable(connector._lease.fd)
        if child:
            with pytest.raises(ProcessLookupError):
                os.kill(child, 0)
    finally:
        state.release.set()
        assert connector.stop(timeout=30, announce=False)
        rig.bus._session._http.close()
        rig.log.close()


@pytest.mark.parametrize("reaction_http", ["https"], indirect=True)
@pytest.mark.parametrize("variable", ["REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"])
@pytest.mark.parametrize("location", ["ca_file", "ca_dir"])
def test_reaction_preserves_ca_files_and_directories(reaction_http, monkeypatch, variable, location):
    client, state = reaction_http
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    monkeypatch.setenv(variable, str(getattr(state, location)))
    client.add_reaction("C1", "1.23", "inbox_tray")
    assert len(state.calls) == 1


@pytest.mark.parametrize("reaction_http", ["https"], indirect=True)
def test_reaction_still_rejects_an_untrusted_certificate(reaction_http, monkeypatch):
    client, state = reaction_http
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", slack_client.requests.certs.where())
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    with pytest.raises(slack_client.SlackError, match="Reaction request failed"):
        client.add_reaction("C1", "1.23", "inbox_tray")
    assert not state.calls
