"""Asking every project assistant to rotate itself (issue #646 item 2)."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time

from fastapi.testclient import TestClient
import pytest

from lmer_platform import api, comms, config, projects, registry, rotate_requests, runs
from lmer_platform import project_context as scope
from tests.test_platform_comms import BusPlane, bearer, live_session
from tests.test_platform_projects import fleet  # noqa: F401


@pytest.fixture
def client(fleet):
    projects.migrate()
    secret = config.ensure_secret(fleet)
    app = api.create_app(fleet, secret, state_builder=lambda *_a, **_k: {
        "runs": [row.to_dict() for row in runs.list_tracked()], "sessions": [], "attention": [],
    })
    with TestClient(app, headers=bearer(secret)) as client:
        yield client
    hub = comms.get_hub()
    if hub:
        hub.stop()


def _running_session(workspace_id):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = projects.status(workspace_id)
        if status["running"] and status["session_id"]:
            return status["session_id"]
        time.sleep(0.05)
    raise AssertionError(f"{workspace_id}'s assistant never started")


def _on_a_bus(plane, session_id):
    """Point an assistant's registry entry at a real bus, keeping what it is."""
    entry = registry.read_session(session_id)
    live_session(plane, session_id, kind=entry["kind"], pid=entry["pid"], run=entry.get("run"), task=entry.get("task"))


class AnsweringPlane:
    """A control plane whose ``/messages`` answers every post with one status."""

    def __init__(self, status, detail):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("content-length") or 0))
                body = json.dumps({"detail": detail}).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class ClosedPlane:
    """A port nothing listens on: a container that has gone.

    Held bound and never listening until :meth:`stop`, so a connect is refused
    and no other test can take the port meanwhile.
    """

    def __init__(self):
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.1", 0))
        self.port = self._socket.getsockname()[1]

    def stop(self):
        self._socket.close()


def test_rotate_all_asks_each_running_assistant_with_the_reason(client, tmp_path):
    alpha, beta = [client.post("/api/projects", json={"name": name}).json() for name in ("Alpha", "Beta")]
    sessions = [_running_session(alpha["id"]), _running_session(beta["id"])]
    planes = [BusPlane(tmp_path, session) for session in sessions]
    try:
        for plane, session in zip(planes, sessions):
            _on_a_bus(plane, session)

        listed = client.get("/api/assistant/rotate-all")
        assert listed.status_code == 200, listed.text
        assert {(t["project_name"], t["generation"], t["running"]) for t in listed.json()["targets"] if t["running"]} == {
            ("Alpha", 1, True), ("Beta", 1, True)}

        reply = client.post("/api/assistant/rotate-all", json={"reason": "new\nstanding orders"})
        assert reply.status_code == 200, reply.text
        body = reply.json()
        assert body["reason"] == "new standing orders"
        assert {t["project_name"]: t["outcome"] for t in body["targets"]} == {
            "Alpha": rotate_requests.REQUESTED, "Beta": rotate_requests.REQUESTED,
            "General": rotate_requests.NOT_RUNNING}
        requested = sorted((t for t in body["targets"] if t["outcome"] == rotate_requests.REQUESTED),
                           key=lambda t: t["project_name"])
        for plane, target in zip(planes, requested, strict=True):
            records, _ = plane.log.read_since(0)
            [record] = [r for r in records if r.source.ref == f"rotate-request:{body['request']}"]
            assert record.id == target["message_id"]
            assert record.source.channel == "platform" and record.source.author == "operator"
            assert body["request"] in record.text and "Reason given: new standing orders" in record.text
            assert "lmer-ctl rotate" in record.text

        # Nothing was stopped: the request is all the platform did.
        for project in (alpha, beta):
            assert projects.status(project["id"])["generation"] == 1
            assert projects.status(project["id"])["lifecycle"] == "running"

        # Each assistant's own `lmer-ctl rotate`: handover, then rotate, on its credential.
        handoffs = {}
        for project in (alpha, beta):
            with scope.bound(project["id"]):
                token = config.active_assistant_credential()
            handoffs[project["id"]] = f"{project['name']} rotated for request {body['request']}: new standing orders"
            assert client.post(
                "/api/assistant/handoff", headers=bearer(token), json={"handoff": handoffs[project["id"]]},
            ).status_code == 200
            rotated = client.post("/api/assistant/rotate", headers=bearer(token), json={})
            assert rotated.status_code == 200, rotated.text
        for project in (alpha, beta):
            assert projects.status(project["id"])["generation"] == 2
            with scope.bound(project["id"]):
                successor = config.active_assistant_credential()
            # Its own project's handover, not the other one written beside it.
            read = client.get("/api/assistant/handoff", headers=bearer(successor))
            assert read.json()["handoff"] == handoffs[project["id"]]
    finally:
        for plane in planes:
            plane.stop()


def test_rotate_all_skips_a_stopped_assistant_and_asks_with_no_reason(client):
    alpha, beta = [client.post("/api/projects", json={"name": name}).json() for name in ("Alpha", "Beta")]
    session = _running_session(alpha["id"])
    _running_session(beta["id"])
    assert client.post("/api/assistant/stop", json={"workspace_id": beta["id"]}).status_code == 200
    plane = AnsweringPlane(404, "Not Found")
    try:
        _on_a_bus(plane, session)
        body = client.post("/api/assistant/rotate-all", json={}).json()
    finally:
        plane.stop()
    [stopped] = [t for t in body["targets"] if t["project_name"] == "Beta"]
    assert (stopped["outcome"], stopped["message_id"], stopped["detail"]) == (rotate_requests.NOT_RUNNING, None, None)
    assert body["reason"] is None
    assert "No reason was given." in body["text"]


@pytest.mark.parametrize("make_plane, outcome, detail", [
    # An image older than the bus: its control plane has no /messages.
    (lambda: AnsweringPlane(404, "Not Found"), rotate_requests.NOT_REACHED, "this session has no message bus"),
    (lambda: AnsweringPlane(503, "no bus"), rotate_requests.NOT_REACHED, "this session has no message bus"),
    (lambda: AnsweringPlane(400, "text refused"), rotate_requests.FAILED, "bus refused the record (text refused)"),
    (ClosedPlane, rotate_requests.FAILED, "its session did not answer"),
], ids=["no-bus-404", "no-bus-503", "refused", "gone"])
def test_rotate_all_reports_each_way_a_request_can_miss(client, make_plane, outcome, detail):
    alpha = client.post("/api/projects", json={"name": "Alpha"}).json()
    session = _running_session(alpha["id"])
    plane = make_plane()
    try:
        _on_a_bus(plane, session)
        body = client.post("/api/assistant/rotate-all", json={"reason": "drill"}).json()
    finally:
        plane.stop()
    [target] = [t for t in body["targets"] if t["project_name"] == "Alpha"]
    assert target["outcome"] == outcome, target
    assert detail in target["detail"], target
    assert target["message_id"] is None
    # Asking is all it does, whatever the answer.
    assert projects.status(alpha["id"])["lifecycle"] == "running"


@pytest.mark.parametrize("reason", [
    "x" * (rotate_requests.MAX_REASON_CHARS + 1), "erase\x08this", "flip\u202eit", 42,
])
def test_rotate_all_refuses_an_unusable_reason(client, reason):
    client.post("/api/projects", json={"name": "Alpha"})
    assert client.post("/api/assistant/rotate-all", json={"reason": reason}).status_code == 400


def test_a_lone_surrogate_reason_is_a_400_not_a_500(client):
    client.post("/api/projects", json={"name": "Alpha"})
    # Raw JSON: a client encoder would refuse to send the surrogate that a browser's JSON.stringify emits.
    reply = client.post(
        "/api/assistant/rotate-all", content=b'{"reason": "\\ud800x"}', headers={"content-type": "application/json"},
    )
    assert reply.status_code == 400, reply.text
    assert "non-printing" in reply.text


def test_rotate_all_is_operator_only(client, fleet):
    alpha = client.post("/api/projects", json={"name": "Alpha"}).json()
    with scope.bound(alpha["id"]):
        token = config.active_assistant_credential()
    connector = config.mint_connector_credential("slack-bridge", "slack")
    for credential in (token, connector):
        assert client.get("/api/assistant/rotate-all", headers=bearer(credential)).status_code == 403
        assert client.post("/api/assistant/rotate-all", headers=bearer(credential), json={}).status_code == 403
    assert projects.status(alpha["id"])["generation"] == 1
