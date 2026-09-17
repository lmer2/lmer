"""Mention and DM-reply ingress through real listeners and the platform API."""
from __future__ import annotations

import json
import os
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from lmer_cli import assistant_mentions as routing, dm_parents, thread_access, thread_posts
from lmer_cli.threads import parse_thread
from lmer_platform import api, assistant, connectors, mention_routing, registry, store
from lmer_platform import config as cfg
from matrix_chat import listener as matrix_listener
from matrix_chat.client import MatrixAccount, MatrixClient, MatrixError
from matrix_chat.permalink import matrix_to_link
from slack_chat import listener as slack_listener
from slack_chat import registry as thread_registry
from slack_chat.client import SlackClient, SlackError
from slack_chat.permalink import slack_thread_link
from tests.conftest import strip_lmer_env

SECRET = "test-secret-value"
AUTH = {"Authorization": f"Bearer {SECRET}"}
SLACK_USER = "U0123ABCD"
MATRIX_USER = "@user:example.org"
ROOM = "!room:example.org"
DM = "D0123ABCD"
STAMP = "1700000002.000000"
ROOT = "1700000000.000000"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    strip_lmer_env(monkeypatch)
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    monkeypatch.setattr(dm_parents, "REGISTRY_DIR", str(tmp_path / "dm-parents"))
    monkeypatch.setattr(thread_posts, "REGISTRY_DIR", str(tmp_path / "posts"))
    monkeypatch.setattr(thread_registry, "REGISTRY_DIR", str(tmp_path / "threads"))
    monkeypatch.setenv(routing.ENABLED_ENV, "true")
    monkeypatch.setenv(routing.BACKLOG_ENV, "2")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("LMER_SLACK_DM_ALLOWED_USERS", SLACK_USER)
    monkeypatch.setenv("LMER_MATRIX_HOMESERVER", "https://example.org")
    monkeypatch.setenv("LMER_MATRIX_USER_ID", "@bot:example.org")
    monkeypatch.setenv("LMER_MATRIX_ACCESS_TOKEN", "test-access")
    monkeypatch.setenv("LMER_MATRIX_ALLOWED_USERS", MATRIX_USER)
    component = connectors.ThreadConnectors()
    monkeypatch.setattr(connectors, "_CONNECTORS", component)
    monkeypatch.setattr(connectors, "ensure_connectors", lambda: component)
    return component


class Slack:
    def __init__(self):
        self.history_calls = []
        self.posts = []
        self.handles = {SLACK_USER: "operator"}

    def workspace(self):
        return {"host": "example.slack.com", "scopes": ["chat:write", "users:read"]}

    def history_before(self, conversation, before, limit):
        self.history_calls.append((conversation, before, limit))
        return [{"ts": f"170000000{i}.000000", "user": SLACK_USER, "text": f"before {i}"}
                for i in range(2)][-limit:] if limit else []

    def _request(self, method, path, **kwargs):
        assert path == "users.info"
        sender = kwargs["params"]["user"]
        return {"ok": True, "user": {"id": sender, "name": self.handles.get(sender, "somebody")}}

    def get_replies(self, channel, ts, oldest=None):
        return [{"ts": ts, "user": SLACK_USER, "text": "root"}]

    def post_message(self, channel, thread_ts, text):
        self.posts.append((channel, thread_ts, text))
        return "1700000003.000000"

    def open_dm(self, user_id):
        return DM

    def post_root(self, channel, text):
        self.posts.append((channel, None, text))
        return {"channel": channel, "ts": ROOT}


class Matrix:
    def __init__(self):
        self.history_calls = []
        self.posts = []
        self.direct_rooms = set()

    def is_direct_room(self, room):
        return room in self.direct_rooms

    def history_before(self, conversation, before, limit):
        self.history_calls.append((conversation, before, limit))
        return [matrix_event(event_id=f"$before{i}", text=f"before {i}") for i in range(2)][-limit:] if limit else []

    def resolve_room(self, room):
        return room

    def get_event(self, room, event):
        return matrix_event(event_id=event)

    def room_is_encrypted(self, room):
        return False

    def thread_events(self, room, event, **kwargs):
        return []

    def send_thread_text(self, room, root, text, **kwargs):
        self.posts.append((room, root, text))
        return "$answer"

    def direct_room(self, user):
        return ROOM

    def send_message(self, room, content):
        self.posts.append((room, None, content["body"]))
        return "$root"


def slack_event(*, root=None, sender=SLACK_USER, kind="app_mention", text="help with this"):
    event = {"type": kind, "user": sender, "channel": "C0123ABCD", "ts": STAMP, "text": text}
    if root:
        event["thread_ts"] = root
    return event


def matrix_event(*, root=None, sender=MATRIX_USER, event_id="$request", text="help with this", mention=True):
    content = {"body": text, "msgtype": "m.text"}
    if mention:
        content["m.mentions"] = {"user_ids": ["@bot:example.org"]}
    if root:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": root}
    return {"type": "m.room.message", "sender": sender, "event_id": event_id, "content": content}


@pytest.fixture
def transports(monkeypatch):
    slack, matrix = Slack(), Matrix()
    monkeypatch.setattr(thread_access, "_slack_client", lambda credential: slack)
    monkeypatch.setattr(thread_access, "_matrix_client", lambda account: matrix)
    return {"slack": slack, "matrix": matrix}


@pytest.fixture
def client(transports):
    return TestClient(api.create_app(cfg.load(), SECRET))


def body(transport, *, threaded=False, dm=False, mention=True):
    event = slack_event(root=ROOT if threaded else None) if transport == "slack" else matrix_event(
        root="$root" if threaded else None, mention=mention)
    conversation = (DM if dm else "C0123ABCD") if transport == "slack" else ROOM
    if dm and transport == "slack":
        event.update(type="message", channel_type="im", channel=conversation)
    return {"transport": transport, "conversation": conversation, "event": event}


def submit(client, value):
    response = client.post("/api/assistant/requests", json=value, headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def pending(client):
    response = client.post("/api/assistant/pending", headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()["pending"]


def remember(transport, *, session="assistant-old"):
    link = slack_thread_link("example.slack.com", DM, ROOT) if transport == "slack" else matrix_to_link(ROOM, "$root")
    dm_parents.remember({"conversation": {"permalink": link, "recipient": SLACK_USER if transport == "slack" else MATRIX_USER}}, session)
    return link


@pytest.mark.parametrize("raw,enabled", [(None, False), ("", False), ("TRUE", True), ("yes", True),
                                        ("1", True), ("on", True), ("0", False), ("OFF", False), ("no", False)])
def test_settings(raw, enabled):
    env = {} if raw is None else {routing.ENABLED_ENV: raw}
    settings = routing.Settings.from_env(env)
    assert settings.enabled is enabled
    assert settings.backlog == 10


@pytest.mark.parametrize("env", [{routing.ENABLED_ENV: "maybe"}, {routing.BACKLOG_ENV: "-1"},
                                {routing.BACKLOG_ENV: "101"}, {routing.BACKLOG_ENV: "1.5"}])
def test_bad_settings_refused(env):
    with pytest.raises(ValueError):
        routing.Settings.from_env(env)


@pytest.mark.parametrize("transport", ["slack", "matrix"])
@pytest.mark.parametrize("threaded", [False, True])
def test_mentions_arrive_with_source_and_correct_context(client, transports, transport, threaded):
    registry.register("assistant-current", kind="assistant", pid=os.getpid())
    assistant._write_state(replace(assistant.read_state(), session_id="assistant-current"))
    request = body(transport, threaded=threaded)
    reply = submit(client, request)
    assert reply["status"] == "accepted"
    assert reply["session"] == "assistant-current"
    notes = pending(client)
    assert len(notes) == 1 and notes[0]["kind"] == "conversation_request"
    data = notes[0]["data"]
    assert data["source"]["channel"] == transport
    assert data["sender"] == (SLACK_USER if transport == "slack" else MATRIX_USER)
    assert data["permalink"] == reply["permalink"]
    assert parse_thread(data["permalink"]).registry_key() == (data["conversation"], data["root"])
    assert data["message"]["text"] == "help with this"
    if threaded:
        assert transports[transport].history_calls == [] and data["backlog"] == []
    else:
        assert len(data["backlog"]) == 2
        assert transports[transport].history_calls == [(data["conversation"], data["message"]["id"], 2)]
    assert transports[transport].posts == []


@pytest.mark.parametrize("threaded", [False, True])
def test_allowlisted_slack_channel_mentions_are_admitted(client, threaded):
    assert submit(client, body("slack", threaded=threaded))["status"] == "accepted"
    assert len(pending(client)) == 1


@pytest.mark.parametrize("transport", ["slack", "matrix"])
@pytest.mark.parametrize("threaded", [False, True])
@pytest.mark.parametrize("allowed", [None, "some-other-user"])
def test_mentions_require_the_transport_allow_list(
    client, monkeypatch, caplog, transport, threaded, allowed,
):
    caplog.set_level("INFO", logger=mention_routing.__name__)
    variable = (
        "LMER_SLACK_DM_ALLOWED_USERS"
        if transport == "slack"
        else "LMER_MATRIX_ALLOWED_USERS"
    )
    if allowed is None:
        monkeypatch.delenv(variable, raising=False)
    else:
        monkeypatch.setenv(variable, allowed)

    assert submit(client, body(transport, threaded=threaded))["status"] == "ignored"
    assert pending(client) == []
    assert variable in caplog.text
    assert "help with this" not in caplog.text


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_replay_after_consumption_and_app_restart_is_not_a_second_request(client, transport):
    request = body(transport)
    assert submit(client, request)["status"] == "accepted"
    assert len(pending(client)) == 1
    restarted = TestClient(api.create_app(cfg.load(), SECRET))
    assert submit(restarted, request)["status"] == "duplicate"
    assert pending(restarted) == []


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_disabled_api_does_not_read_transport_or_spawn(monkeypatch, transports, transport):
    monkeypatch.setenv(routing.ENABLED_ENV, "false")
    client = TestClient(api.create_app(cfg.load(), SECRET))
    assert submit(client, body(transport))["status"] == "disabled"
    assert pending(client) == []
    assert transports[transport].history_calls == []


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_assistant_dm_parent_survives_rotation_and_is_required(client, transport):
    request = body(transport, threaded=True, dm=True, mention=False)
    assert submit(client, request)["status"] == "ignored"
    remember(transport)
    assert submit(client, request)["status"] == "accepted"
    data = pending(client)[0]["data"]
    assert data["entry"] == "dm_reply"
    assert data["parent"]["session"] == "assistant-old"
    assert data["parent"]["root"] == data["root"]
    assert submit(client, request)["status"] == "duplicate"


@pytest.mark.parametrize("transport", ["slack", "matrix"])
@pytest.mark.parametrize("allowed", ["", "some-other-user"])
def test_dm_sender_admission_is_deny_by_default(client, monkeypatch, caplog, transport, allowed):
    caplog.set_level("INFO", logger=mention_routing.__name__)
    remember(transport)
    monkeypatch.setenv("LMER_SLACK_DM_ALLOWED_USERS" if transport == "slack" else "LMER_MATRIX_ALLOWED_USERS", allowed)
    request = body(transport, threaded=True, dm=True, mention=False)
    assert submit(client, request)["status"] == "ignored"
    assert pending(client) == []
    assert "denied" in caplog.text
    assert "help with this" not in caplog.text


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_another_thread_in_same_dm_channel_never_routes_to_assistant(client, transport):
    remember(transport)
    request = body(transport, threaded=True, dm=True, mention=False)
    root = "1700000001.000000" if transport == "slack" else "$other-run"
    if transport == "slack":
        request["event"]["thread_ts"] = root
    else:
        request["event"]["content"]["m.relates_to"]["event_id"] = root
    thread_registry.register(request["conversation"], root, kind=transport, owner="chat-run")
    assert submit(client, request)["status"] in {"ignored", "connected"}
    assert pending(client) == []
    assert thread_registry.thread_holder(request["conversation"], root, kind=transport)["owner"] == "chat-run"


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_unthreaded_dm_message_ignored(client, transport):
    remember(transport)
    assert submit(client, body(transport, dm=True, mention=False))["status"] == "ignored"
    assert pending(client) == []


def test_matrix_rich_reply_without_m_thread_does_not_match_dm_parent(client):
    remember("matrix")
    request = body("matrix", dm=True, mention=False)
    request["event"]["content"]["m.relates_to"] = {"m.in_reply_to": {"event_id": "$root"}}
    assert submit(client, request)["status"] == "ignored"
    assert pending(client) == []


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_recorded_connection_precedes_running_connector(client, isolated, transport):
    request = body(transport)
    event_id = request["event"].get("ts") or request["event"]["event_id"]
    link = slack_thread_link("example.slack.com", request["conversation"], event_id) if transport == "slack" else matrix_to_link(ROOM, event_id)
    ref = parse_thread(link)
    registry.register("worker", kind="worker", pid=os.getpid())
    isolated._sessions["worker"] = [{"id": ref.id}]
    assert submit(client, request)["status"] == "connected"
    assert pending(client) == []


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_direct_reply_uses_only_destination_and_no_comms_bus(client, transports, monkeypatch, transport):
    submit(client, body(transport))
    data = pending(client)[0]["data"]
    monkeypatch.setattr(api.comms, "ensure_hub", lambda: pytest.fail("conversation contents reached bus"))
    response = client.post("/api/comms/thread", json={"permalink": data["permalink"], "text": "answer here"}, headers=AUTH)
    assert response.status_code == 200, response.text
    posted = transports[transport].posts
    assert len(posted) == 1
    assert posted[0][:2] == (data["conversation"], data["root"])
    assert "answer here" in posted[0][2]
    assert transports["matrix" if transport == "slack" else "slack"].posts == []


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_successful_assistant_dm_send_records_exact_parent(transports, transport):
    servicer = thread_access.ThreadServicer(supervisor_url="http://localhost", token=SECRET, assistant_session=True)
    servicer._session = "assistant-current"
    recipient = SLACK_USER if transport == "slack" else MATRIX_USER
    result = servicer.handle({"op": "dm", "recipient": recipient, "text": "private outgoing message"})
    ref = parse_thread(result["conversation"]["permalink"])
    recorded = dm_parents.lookup(transport, *ref.registry_key())
    assert recorded["session"] == "assistant-current"
    assert "private outgoing message" not in json.dumps(recorded)


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_worker_dm_send_cannot_establish_assistant_parent(transports, transport):
    servicer = thread_access.ThreadServicer(supervisor_url="http://localhost", token=SECRET)
    servicer._session = "worker"
    result = servicer.handle({"op": "dm", "recipient": SLACK_USER if transport == "slack" else MATRIX_USER, "text": "worker message"})
    ref = parse_thread(result["conversation"]["permalink"])
    assert dm_parents.lookup(transport, *ref.registry_key()) is None


def test_full_inbox_refuses_without_remembering_and_normal_notifications_do_not_evict_request(client, monkeypatch):
    monkeypatch.setattr(assistant, "MAX_PENDING", 1)
    first = body("slack")
    assert submit(client, first)["status"] == "accepted"
    assistant.notify("ordinary lifecycle event")
    assert assistant.read_state().pending[0].kind == "conversation_request"
    second = body("matrix")
    response = client.post("/api/assistant/requests", json=second, headers=AUTH)
    assert response.status_code == 429
    assert len(assistant.read_state().conversation_seen) == 1
    pending(client)
    assert submit(client, second)["status"] == "accepted"


def test_persistence_failure_does_not_claim_acceptance(client, monkeypatch):
    monkeypatch.setattr(assistant, "_write_state", lambda state: False)
    response = client.post("/api/assistant/requests", json=body("slack"), headers=AUTH)
    assert response.status_code >= 400
    assert assistant.read_state().conversation_seen == ()


def test_route_requires_operator_credential(client, monkeypatch):
    assert client.post("/api/assistant/requests", json=body("slack")).status_code == 401
    monkeypatch.setattr(api, "active_assistant_credential", lambda: "assistant-secret")
    response = client.post("/api/assistant/requests", json=body("slack"), headers={"Authorization": "Bearer assistant-secret"})
    assert response.status_code == 403


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_failed_history_does_not_mark_request_delivered(client, transports, transport):
    error = SlackError("history unavailable") if transport == "slack" else MatrixError("history unavailable")
    transports[transport].history_before = MagicMock(side_effect=error)
    response = client.post("/api/assistant/requests", json=body(transport), headers=AUTH)
    assert response.status_code == 502
    assert assistant.read_state().conversation_seen == ()


def test_slack_history_paginated_and_strictly_before_trigger(monkeypatch):
    client = SlackClient(bot_token="xoxb-test")
    calls = []
    pages = iter([
        {"messages": [{"ts": "2.0"}], "response_metadata": {"next_cursor": "page2"}},
        {"messages": [{"ts": "1.0"}], "response_metadata": {}},
    ])

    def request(method, path, **kwargs):
        calls.append((path, dict(kwargs["params"])))
        return next(pages)

    monkeypatch.setattr(client, "_request", request)
    assert [item["ts"] for item in client.history_before("C123", "3.0", 2)] == ["1.0", "2.0"]
    assert all(params["latest"] == "3.0" and params["inclusive"] is False for _, params in calls)
    assert calls[1][1]["cursor"] == "page2"
    assert client.history_before("C123", "3.0", 0) == []
    assert len(calls) == 2


def test_matrix_history_uses_event_boundary_not_clock(monkeypatch):
    client = MatrixClient(MatrixAccount("https://example.org", "@bot:example.org", "test-access"))
    calls = []
    pages = iter([
        {"start": "before-trigger", "events_after": [matrix_event(event_id="$later")]},
        {"chunk": [matrix_event(event_id="$second")], "end": "older"},
        {"chunk": [matrix_event(event_id="$first")], "end": None},
    ])

    def call(method, path, **kwargs):
        calls.append((path, dict(kwargs["params"])))
        return next(pages)

    monkeypatch.setattr(client, "_call", call)
    assert [item["event_id"] for item in client.history_before(ROOM, "$trigger", 2)] == ["$first", "$second"]
    assert calls[0][0].endswith("/context/%24trigger")
    assert calls[0][1]["limit"] == 0
    assert calls[1][1]["from"] == "before-trigger" and calls[1][1]["dir"] == "b"
    assert client.history_before(ROOM, "$trigger", 0) == []
    assert len(calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_slack_listener_switches_mention_routing(monkeypatch, enabled):
    monkeypatch.setattr(slack_listener, "MENTIONS", routing.Settings(enabled))
    manager = MagicMock()
    manager.touch.return_value = False
    monkeypatch.setattr(slack_listener, "session_manager", manager)
    monkeypatch.setattr(slack_listener, "is_thread_connected", lambda *args: False)
    forward = MagicMock(return_value={"status": "accepted"})
    spawn = AsyncMock()
    monkeypatch.setattr(routing, "forward", forward)
    monkeypatch.setattr(slack_listener, "_connect_lmer_session", spawn)
    event = slack_event()
    await slack_listener.handle_mention(event, AsyncMock())
    assert forward.call_count == int(enabled)
    assert spawn.await_count == int(not enabled)


@pytest.mark.asyncio
async def test_slack_connected_dm_reply_stays_with_worker(monkeypatch, client):
    monkeypatch.setattr(slack_listener, "MENTIONS", routing.Settings(True))
    manager = MagicMock()
    manager.touch.return_value = False
    monkeypatch.setattr(slack_listener, "session_manager", manager)
    remember("slack")
    thread_registry.register(DM, ROOT, owner="chat-run")
    forward = MagicMock(side_effect=lambda transport, conversation, event: submit(client, {
        "transport": transport, "conversation": conversation, "event": event}))
    monkeypatch.setattr(routing, "forward", forward)
    request = body("slack", threaded=True, dm=True)
    await slack_listener.handle_message_event(request["event"], AsyncMock())
    forward.assert_not_called()
    manager.touch.assert_called_once_with(DM, ROOT)
    assert pending(client) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_matrix_listener_switches_mention_routing(monkeypatch, enabled):
    monkeypatch.setenv(routing.ENABLED_ENV, str(enabled))
    sessions = MagicMock()
    sessions.touch.return_value = False
    sessions.at_capacity.return_value = False
    sessions.spawn = AsyncMock()
    sessions.default_preset.return_value = (None, None)
    fake = Matrix()
    listener = matrix_listener.Listener(matrix_listener.ListenerConfig(), sessions, matrix=fake)
    forward = MagicMock(return_value={"status": "accepted"})
    monkeypatch.setattr(routing, "forward", forward)
    await listener.handle(ROOM, matrix_event())
    assert forward.call_count == int(enabled)
    assert sessions.spawn.await_count == int(not enabled)


@pytest.mark.parametrize("enabled", [False, True])
def test_matrix_sync_admits_threaded_dm_replies_only_in_routing_mode(monkeypatch, enabled):
    monkeypatch.setenv(routing.ENABLED_ENV, str(enabled))
    event = matrix_event(root="$root", mention=False)
    matrix = SimpleNamespace(sync=lambda **kwargs: {"next_batch": "after", "rooms": {"join": {
        ROOM: {"timeline": {"events": [event, matrix_event(event_id="$plain", mention=False)]}}}}})
    listener = matrix_listener.Listener(matrix_listener.ListenerConfig(), MagicMock(), matrix=matrix)
    listener._next_batch = "before"
    assert listener.sync_once() == ([(ROOM, event)] if enabled else [])


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_context_destination_can_be_attached_to_a_worker(client, isolated, monkeypatch, transports, transport):
    from tests.test_platform_thread_routes import live_session

    submit(client, body(transport))
    data = pending(client)[0]["data"]
    live_session("chosen-worker")
    isolated._slack_client = lambda credential: transports["slack"]
    isolated._matrix_client = lambda account: transports["matrix"]
    started = []
    monkeypatch.setattr(isolated, "_ensure", lambda key, record, **kwargs: started.append((key, record)))
    reply = client.post("/api/sessions/chosen-worker/threads", json={"permalink": data["permalink"]}, headers=AUTH)
    assert reply.status_code == 201, reply.text
    record = reply.json()["thread"]
    assert record["id"] == parse_thread(data["permalink"]).id
    assert started[0][0] == ("chosen-worker", record["id"])
    assert isolated.live_holder(parse_thread(data["permalink"])) == "chosen-worker"
    assert submit(client, body(transport))["status"] == "connected"
    assert pending(client) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["slack", "matrix"])
async def test_listener_through_http_to_assistant_pending(client, monkeypatch, transport):
    from lmer_platform import client as platform_client

    monkeypatch.setattr(cfg, "read_secret", lambda config: SECRET)
    original_request = platform_client.request
    calls = []

    def request(endpoint, call, **kwargs):
        calls.append(call.path)
        return original_request(endpoint, call, transport=client, **kwargs)

    monkeypatch.setattr(platform_client, "request", request)
    sessions = MagicMock()
    sessions.touch.return_value = False
    if transport == "slack":
        monkeypatch.setattr(slack_listener, "MENTIONS", routing.Settings(True))
        monkeypatch.setattr(slack_listener, "session_manager", sessions)
        await slack_listener.handle_mention(slack_event(), AsyncMock())
    else:
        listener = matrix_listener.Listener(matrix_listener.ListenerConfig(), sessions)
        await listener.handle(ROOM, matrix_event())
    assert calls == ["/api/assistant/requests"]
    assert pending(client)[0]["data"]["source"]["channel"] == transport
    sessions.spawn.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["slack", "matrix"])
async def test_other_dm_thread_reply_reaches_worker_bus_only(tmp_path, client, monkeypatch, transport):
    from tests.test_slack_connector import Rig as SlackRig
    from tests.test_matrix_chat import Rig as MatrixRig
    from matrix_chat import connector as matrix_connector
    from slack_chat import connector as slack_connector

    remember(transport)
    rig = SlackRig(tmp_path) if transport == "slack" else MatrixRig(tmp_path)
    sessions = MagicMock()
    sessions.touch.return_value = False
    try:
        if transport == "slack":
            rig.config = replace(rig.config, channel=DM)
            rig.connector = slack_connector.SlackConnector(rig.config, slack=rig.slack, bus_client=rig.bus)
            rig.connector._poll_thread(replay=True)
            ts = rig.slack.human_says("for the worker", user=SLACK_USER)
            event = {"type": "message", "channel_type": "im", "channel": DM,
                     "thread_ts": rig.config.thread_ts, "ts": ts, "user": SLACK_USER, "text": "for the worker"}
            monkeypatch.setattr(slack_listener, "MENTIONS", routing.Settings(True))
            monkeypatch.setattr(slack_listener, "session_manager", sessions)
            await slack_listener.handle_message_event(event, AsyncMock())
            assert rig.connector._poll_thread() == 1
        else:
            rig.config = replace(rig.config, room=ROOM)
            rig.connector = matrix_connector.MatrixConnector(rig.config, matrix=rig.matrix, bus_client=rig.bus)
            rig.connector.replay()
            event = rig.matrix.human_says("for the worker", sender=MATRIX_USER)
            listener = matrix_listener.Listener(matrix_listener.ListenerConfig(), sessions)
            await listener.handle(ROOM, event)
            assert rig.connector.sync_once() == 1
        assert rig.log.read_since(0)[0][-1].text == "for the worker"
        assert b"for the worker" in b"".join(rig.sink)
        assert pending(client) == []
    finally:
        assert rig.connector.stop(timeout=10, announce=False)
        rig.bus._session._http.close()
        rig.log.close()


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_concurrent_duplicate_is_stored_once(client, transport):
    from concurrent.futures import ThreadPoolExecutor

    request = body(transport)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(client, request)["status"], range(2)))
    assert sorted(results) == ["accepted", "duplicate"]
    assert len(pending(client)) == 1


@pytest.mark.parametrize("raw", ["operator", "@OPERATOR"])
def test_slack_dm_confirmed_handle_is_admitted(client, monkeypatch, raw):
    remember("slack")
    monkeypatch.setenv("LMER_SLACK_DM_ALLOWED_USERS", raw)
    assert submit(client, body("slack", threaded=True, dm=True))["status"] == "accepted"


def test_slack_handle_cannot_impersonate_id_entry(client, monkeypatch, transports):
    remember("slack")
    transports["slack"].handles["UOTHER999"] = SLACK_USER.lower()
    request = body("slack", threaded=True, dm=True)
    request["event"]["user"] = "UOTHER999"
    assert submit(client, request)["status"] == "ignored"


def test_slack_failed_handle_lookup_denies(client, monkeypatch, transports):
    remember("slack")
    monkeypatch.setenv("LMER_SLACK_DM_ALLOWED_USERS", "operator")
    transports["slack"]._request = MagicMock(side_effect=SlackError("lookup unavailable"))
    assert submit(client, body("slack", threaded=True, dm=True))["status"] == "ignored"


@pytest.mark.parametrize("kind,task,expected", [("assistant", "orchestrate", True),
                                              ("worker", "orchestrate", False),
                                              ("assistant", "custom-assistant", True)])
def test_host_assigned_kind_controls_dm_receipts(monkeypatch, kind, task, expected):
    from lmer_cli import cli

    monkeypatch.setenv("LMER_SESSION_KIND", kind)
    constructor = MagicMock()
    monkeypatch.setattr(thread_access, "ThreadServicer", constructor)
    cli._start_thread_servicer(SimpleNamespace(session="s-1"), 8000, SECRET, None, None, task)
    assert constructor.call_args.kwargs["assistant_session"] is expected
    constructor.return_value.start.assert_called_once()


def test_assistant_identity_is_required_before_dm_send(transports, monkeypatch):
    servicer = thread_access.ThreadServicer(supervisor_url="http://localhost", token=SECRET, assistant_session=True)
    monkeypatch.setattr(servicer, "resolved_session", lambda: None)
    with pytest.raises(thread_access.ThreadError, match="no direct message was sent"):
        servicer.handle({"op": "dm", "recipient": SLACK_USER, "text": "private"})
    assert transports["slack"].posts == []


def test_receipt_write_failure_says_the_dm_was_already_sent(transports, monkeypatch):
    servicer = thread_access.ThreadServicer(supervisor_url="http://localhost", token=SECRET, assistant_session=True)
    servicer._session = "assistant"
    monkeypatch.setattr(dm_parents, "remember", MagicMock(side_effect=OSError("disk full")))
    with pytest.raises(thread_access.ThreadError, match="direct message was sent.*do not resend"):
        servicer.handle({"op": "dm", "recipient": SLACK_USER, "text": "private"})
    assert len(transports["slack"].posts) == 1
