"""The daemon launch hands its resolved host endpoint to both connector clients."""
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from lmer_cli import assistant_mentions
from lmer_platform import client as platform_client
from lmer_platform import config, connectors, daemon, session_io
from tests.test_platform_projects import fleet  # noqa: F401
from tests.test_platform_project_transports import transport_host, _link  # noqa: F401
from tests.test_assistant_mentions import body
from tests.test_platform_thread_connectors import Runner, live_session
from tests.connector_fakes import SlackRig
from tests.test_assistant_mentions import SLACK_USER
from ask_channel import bus
from lmer_platform import project_requests
from lmer_cli.connector_delivery import checkpoint_path
from lmer_cli.connector_ownership import read_json
from slack_chat.connector import SlackConnector


@pytest.mark.parametrize("transport", ["slack", "matrix"])
@pytest.mark.parametrize("bind,host", [
    ("127.0.0.2", "127.0.0.2"), ("0.0.0.0", "127.0.0.1"),
    ("::", "[::1]"), ("::1", "[::1]"),
])
def test_cli_endpoint_reaches_connector_forwarding(transport_host, monkeypatch, transport, bind, host):
    api_client, transports, _component = transport_host
    runner = Runner()
    noop = SimpleNamespace(stop=lambda: None)
    for name in ("_supervise_assistant", "_watch_for_attention", "_subscribe_to_comms", "_serve_thread_requests"):
        monkeypatch.setattr(daemon, name, lambda *args: noop)
    monkeypatch.setattr(daemon, "reattach_all", lambda: [])
    monkeypatch.setattr(connectors.ThreadConnectors, "start", lambda self: None)
    monkeypatch.setattr(connectors, "_slack_client", lambda _token: transports["slack"])
    monkeypatch.setattr(connectors, "_matrix_client", lambda _account: transports["matrix"])
    monkeypatch.setattr(connectors, "_spawn_process", runner)
    monkeypatch.setattr(session_io, "quiet_health", lambda _session: {"session": "endpoint-fixture"})
    monkeypatch.setenv("LMER_PLATFORM_URL", "https://unrelated.example.test")
    monkeypatch.setenv(config.ENV_CONTAINER_URL, "https://container.example.test:9443")
    seen = []

    def request(endpoint, call, **kwargs):
        seen.append(endpoint.base_url)
        assert endpoint.base_url == f"http://{host}:7143"
        assert endpoint.credential == config.read_secret(config.load())
        assert endpoint.credential != "control-plane-token"
        assert call.path == "/api/assistant/requests"
        return api_client.request(call.method, call.path, json=call.body,
                                  headers={"Authorization": f"Bearer {endpoint.credential}"})

    monkeypatch.setattr(platform_client, "request", request)

    def serve(_app, **kwargs):
        assert (kwargs["host"], kwargs["port"]) == (bind, 7143)
        event = body(transport, threaded=True)
        live_session("s-endpoint")
        manager = connectors.get_connectors()
        manager.connect("s-endpoint", _link(event), by="operator")
        assert len(runner.started) == 1
        child = runner.started[0]
        assert child.env["LMER_SUPERVISOR_URL"] == "http://127.0.0.1:8123"
        assert child.env[config.ENV_CONTAINER_URL] == "https://container.example.test:9443"
        assert "control-plane-token" not in child.argv
        with patch.dict(os.environ, child.env, clear=True):
            first = assistant_mentions.forward(transport, event["conversation"], event["event"])
            again = assistant_mentions.forward(transport, event["conversation"], event["event"])
        assert first["status"] == "accepted"
        assert again["status"] == "duplicate"
        assert first["request_id"] == again["request_id"]

    monkeypatch.setattr("uvicorn.run", serve)
    try:
        assert daemon.main(["run", "--bind", bind, "--port", "7143"]) == 0
    finally:
        if connectors.get_connectors():
            connectors.get_connectors().stop()
    assert seen == [f"http://{host}:7143"] * 2


def test_retry_restart_and_later_messages_keep_the_original_identity(transport_host, tmp_path, monkeypatch):
    api_client, _transports, _component = transport_host
    rig = SlackRig(tmp_path)
    rig.slack.names = {}
    rig.slack.workspace = lambda: {"host": "example.slack.com"}
    component = rig.connector
    attempts = []
    admitted = []
    mode = ["refused"]

    def request(endpoint, call, **kwargs):
        assert endpoint.base_url == "http://127.0.0.1:7143"
        attempts.append(call.body)
        if mode[0] == "refused":
            raise platform_client.PlatformError("Fixture daemon unavailable before admission")
        response = api_client.request(call.method, call.path, json=call.body, headers=endpoint.headers())
        admitted.append(response.json())
        if mode[0] == "lost-response":
            raise platform_client.PlatformError("Fixture response lost after admission")
        return response

    monkeypatch.setattr(platform_client, "request", request)
    manager = connectors.ThreadConnectors(platform_config=config.load({"bind_port": 7143}))
    environment = manager._environment(SimpleNamespace(host="127.0.0.1", port=8123, token="fixture-control"), "worker", {})
    before = after = None
    try:
        with patch.dict(os.environ, environment, clear=True):
            component._inbound_once()
            cursor = component._cursor_ts
            before = rig.slack.human_says("Ordinary before mention", user=SLACK_USER)
            mention = rig.slack.human_says("<@UBOT> Request a review", user=SLACK_USER)
            after = rig.slack.human_says("Ordinary after mention", user=SLACK_USER)
            with pytest.raises(platform_client.PlatformError, match="before admission"):
                component._inbound_once()
            assert component._cursor_ts == cursor
            assert before in component._accepted_inbound
            assert mention not in component._accepted_inbound
            assert after not in component._accepted_inbound
            mode[0] = "lost-response"
            with pytest.raises(platform_client.PlatformError, match="after admission"):
                component._inbound_once()
            identity = admitted[0]["request_id"]
            assert component._cursor_ts == cursor
            assert len(project_requests.listing()) == 1
            assert component.stop(announce=False)
            component = SlackConnector(rig.config, slack=rig.slack, bus_client=rig.bus)
            component._seed_cursor()
            mode[0] = "healthy"
            original_post = rig.bus.post
            normal_attempts = []

            def lose_normal_receipt(*args, **kwargs):
                result = original_post(*args, **kwargs)
                if kwargs.get("text") == "Ordinary after mention":
                    normal_attempts.append((kwargs, result["message"]["id"]))
                    if len(normal_attempts) == 1:
                        raise bus.BusError("Fixture lost accepted message receipt")
                return result

            monkeypatch.setattr(rig.bus, "post", lose_normal_receipt)
            with pytest.raises(bus.BusError, match="lost accepted"):
                component._inbound_once()
            assert after in component._pending_inbound
            pending = dict(component._pending_inbound[after])
            assert component.stop(announce=False)
            component = SlackConnector(rig.config, slack=rig.slack, bus_client=rig.bus)
            component._seed_cursor()
            component._inbound_once()
            assert component._cursor_ts == after
            assert not component._pending_inbound
            assert normal_attempts[0] == normal_attempts[1]
            assert normal_attempts[1][0] == pending["request"]
            assert admitted[-1]["request_id"] == identity
            assert admitted[-1]["status"] == "duplicate"
            records = rig.log.read_since(0)[0]
            assert sum(row.text == "Ordinary before mention" for row in records) == 1
            assert sum(row.text == "Ordinary after mention" for row in records) == 1
            assert not any("Request a review" in row.text for row in records)
            assert len(project_requests.listing()) == 1
    finally:
        component.stop(announce=False)
        rig.bus._session._http.close()
        rig.log.close()


def test_failure_health_survives_restart_and_does_not_wake_models(transport_host, tmp_path, monkeypatch):
    _client, _transports, _component = transport_host
    rig = SlackRig(tmp_path)
    rig.slack.names = {}
    rig.slack.workspace = lambda: {"host": "example.slack.com"}
    component = rig.connector
    component._ensure_ready()
    attempts = []

    def fail():
        attempts.append(1)
        if len(attempts) == 3:
            component._stop.set()
        raise RuntimeError("private details must not enter status")

    monkeypatch.setattr(component, "_backoff", lambda: None)
    try:
        component._loop("inbound", fail)
        saved = read_json(checkpoint_path(component._lease.key, component._state.session))
        assert saved["transport"]["_delivery_health"]["inbound"]["failures"] == 3
        assert saved["transport"]["_delivery_health"]["inbound"]["error"] == "RuntimeError"
        manager = connectors.ThreadConnectors()
        record = manager.adopt_launch_targets("worker", ["https://example.slack.com/archives/C1/p1699999999000000"], by="operator")[0]
        manager._procs[("worker", record["id"])] = SimpleNamespace(
            popen=SimpleNamespace(poll=lambda: None), since="fixture", lease=component._lease)
        status = manager.listing("worker")[0]
        assert status["running"] and status["attention"] == "delivery-retrying"
        assert "3 failures" in status["detail"]
        assert "private details" not in str(status)
        assert component.stop(announce=False)
        component = SlackConnector(rig.config, slack=rig.slack, bus_client=rig.bus)
        component._ensure_ready()
        assert component._delivery_health["inbound"]["failures"] == 3
        component._loop("inbound", lambda: component._stop.set())
        assert component._delivery_health["inbound"]["failures"] == 0
        notes = [row for row in rig.log.read_since(0)[0] if row.kind == "note"]
        assert len(notes) == 2
        assert all(row.meta["notification_class"] == "connector_health" for row in notes)
    finally:
        component.stop(announce=False)
        rig.bus._session._http.close()
        rig.log.close()
