"""Review regressions across listener selection, host admission and taught CLI."""
from __future__ import annotations

import os
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from lmer_cli import assistant_mentions as routing, dm_parents, thread_access
from lmer_platform import api, assistant, ctl, registry, thread_service
from lmer_platform import client as platform_client
from lmer_platform import config as cfg
from matrix_chat import listener as matrix_listener
from matrix_chat.client import MatrixAccount, MatrixClient, MatrixError
from slack_chat import listener as slack_listener
from tests.test_assistant_mentions import (
    AUTH, MATRIX_USER, ROOM, SECRET, SLACK_USER,
    body, client, isolated, matrix_event, pending, remember, submit, transports,
)

REPO = Path(__file__).resolve().parent.parent


def through_http(monkeypatch, daemon):
    monkeypatch.setattr(cfg, "read_secret", lambda config: SECRET)
    request = platform_client.request
    monkeypatch.setattr(platform_client, "request", lambda endpoint, call, **kw:
                        request(endpoint, call, transport=daemon, **kw))


async def dispatch(monkeypatch, transports, transport, value):
    sessions = MagicMock()
    sessions.touch.return_value = False
    if transport == "slack":
        monkeypatch.setattr(slack_listener, "MENTIONS", routing.Settings(True))
        monkeypatch.setattr(slack_listener, "session_manager", sessions)
        await slack_listener.handle_mention(value["event"], AsyncMock())
    else:
        matrix = transports["matrix"]
        matrix.sync = lambda **kw: {"next_batch": "after", "rooms": {"join": {
            value["conversation"]: {"timeline": {"events": [value["event"]]}}}}}
        listener = matrix_listener.Listener(matrix_listener.ListenerConfig(), sessions, matrix=matrix)
        listener._next_batch = "before"
        for room, event in listener.sync_once():
            await listener.handle(room, event)
    sessions.spawn.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport,fact", [("slack", "prefix"), ("matrix", "receipt"), ("matrix", "account-data")])
@pytest.mark.parametrize("relation", ["top-level", "other-thread", "rich-reply"])
async def test_dm_mentions_never_enter_assistant_or_fetch_backlog(
    client, monkeypatch, transports, transport, fact, relation,
):
    if fact != "account-data":
        remember(transport)
        if transport == "matrix":
            transports["matrix"].is_direct_room = MagicMock(side_effect=AssertionError("receipt already identifies DM"))
    else:
        transports["matrix"].direct_rooms.add(ROOM)
    value = body(transport, dm=True, mention=True)
    if transport == "slack":
        value["event"]["type"] = "app_mention"
        value["event"]["text"] = "<@UBOT> private request"
        if relation != "top-level":
            value["event"]["thread_ts"] = "1700000001.000000"
    elif relation == "other-thread":
        value["event"]["content"]["m.relates_to"] = {"rel_type": "m.thread", "event_id": "$other"}
    elif relation == "rich-reply":
        value["event"]["content"]["m.relates_to"] = {"m.in_reply_to": {"event_id": "$root"}}
    through_http(monkeypatch, client)
    await dispatch(monkeypatch, transports, transport, value)
    assert pending(client) == []
    assert transports[transport].history_calls == []
    assert submit(client, value)["status"] == "ignored"
    assert pending(client) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["slack", "matrix"])
async def test_matched_dm_reply_with_mention_keeps_parent_and_has_no_backlog(
    client, monkeypatch, transports, transport,
):
    remember(transport)
    value = body(transport, dm=True, threaded=True, mention=True)
    if transport == "slack":
        value["event"]["type"] = "app_mention"
    else:
        transports["matrix"].direct_rooms.add(ROOM)
    through_http(monkeypatch, client)
    await dispatch(monkeypatch, transports, transport, value)
    notes = pending(client)
    assert len(notes) == 1
    assert notes[0]["data"]["entry"] == "dm_reply"
    assert notes[0]["data"]["parent"]["root"] == notes[0]["data"]["root"]
    assert notes[0]["data"]["backlog"] == []
    assert transports[transport].history_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mentioned", ["@botany:example.org", "@bot:example.org.evil", "@bot:elsewhere.org"])
@pytest.mark.parametrize("explicit", [True, False])
async def test_similarly_named_matrix_users_are_not_assistant_mentions(
    client, monkeypatch, transports, mentioned, explicit,
):
    value = body("matrix")
    content = value["event"]["content"]
    content["body"] = f"hello {mentioned}"
    if explicit:
        content["m.mentions"] = {"user_ids": [mentioned]}
    else:
        content.pop("m.mentions")
    through_http(monkeypatch, client)
    forward = MagicMock(wraps=routing.forward)
    monkeypatch.setattr(routing, "forward", forward)
    await dispatch(monkeypatch, transports, "matrix", value)
    forward.assert_not_called()
    assert submit(client, value)["status"] == "ignored"
    assert pending(client) == []
    assert transports["matrix"].history_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["slack", "matrix"])
async def test_enabled_listener_reports_disabled_daemon_without_spawning(
    monkeypatch, transports, caplog, transport,
):
    monkeypatch.setenv(routing.ENABLED_ENV, "false")
    daemon = TestClient(api.create_app(cfg.load(), SECRET))
    monkeypatch.setenv(routing.ENABLED_ENV, "true")
    through_http(monkeypatch, daemon)
    await dispatch(monkeypatch, transports, transport, body(transport))
    assert "assistant_routing_failed" in caplog.text
    assert "disabled" in caplog.text
    assert pending(daemon) == []
    assert transports[transport].history_calls == []


@pytest.mark.parametrize("document", ["docs/ASSISTANT-MENTIONS.md", "taskdef/orchestrate/instructions.txt"])
@pytest.mark.parametrize("permalink", ["https://example.slack.com/archives/C123/p1700000000000000", "https://matrix.to/#/!room:example.org/$root"])
def test_documented_spawn_flag_reaches_the_spawn_request(document, permalink):
    text = (REPO / document).read_text()
    flags = re.findall(r"`(--(?:target-)?thread)(?: <permalink>)?`", text)
    assert len(flags) == 1
    args = ctl.create_parser().parse_args([
        "spawn", "develop", "https://gitlab.example.com/group/repo/-/issues/1",
        flags[0], permalink, "--title", "Requested work",
    ])
    assert args.targets == [permalink]
    assert args.call(args).body["targets"] == [permalink]


@pytest.mark.parametrize("kind", ["assistant", "worker"])
@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_daemon_servicer_gets_receipt_authority_from_registry(
    monkeypatch, transports, kind, transport,
):
    session_id = f"registered-{kind}"
    registry.register(session_id, kind=kind, pid=os.getpid())
    monkeypatch.setattr(thread_service.session_io, "control_endpoint", lambda session:
                        SimpleNamespace(host="127.0.0.1", port=8123, token=SECRET))
    monkeypatch.setattr(thread_access.ThreadServicer, "start", lambda self: None)
    services = thread_service.ThreadServices(environ=dict(os.environ))
    services.ensure(session_id)
    servicer = services._servicers[session_id]
    servicer._session = session_id
    result = servicer.handle({"op": "dm", "recipient": SLACK_USER if transport == "slack" else MATRIX_USER,
                             "text": "a private message"})
    ref = thread_access.parse_thread(result["conversation"]["permalink"])
    receipt = dm_parents.lookup(transport, *ref.registry_key())
    assert (receipt is not None) is (kind == "assistant")
    if receipt:
        assert receipt["session"] == session_id


@pytest.mark.parametrize("mentions", [None, {}, {"user_ids": []}, {"user_ids": ["@other:example.org"]},
                                     {"user_ids": "@bot:example.org"}, {"room": True}])
def test_body_cannot_override_matrix_mention_identities(client, transports, mentions):
    value = body("matrix")
    value["event"]["content"].update(body="> <@bot:example.org> quoted text", **{"m.mentions": mentions})
    assert submit(client, value)["status"] == "ignored"
    assert pending(client) == []
    assert transports["matrix"].history_calls == []


@pytest.mark.parametrize("enabled", [False, True])
def test_disabled_matrix_listener_keeps_legacy_body_recognition(monkeypatch, transports, enabled):
    monkeypatch.setenv(routing.ENABLED_ENV, str(enabled))
    event = matrix_event(text="@botany:example.org", mention=False)
    matrix = transports["matrix"]
    matrix.sync = lambda **kw: {"next_batch": "after", "rooms": {"join": {
        ROOM: {"timeline": {"events": [event]}}}}}
    listener = matrix_listener.Listener(matrix_listener.ListenerConfig(), MagicMock(), matrix=matrix)
    listener._next_batch = "before"
    assert listener.sync_once() == ([] if enabled else [(ROOM, event)])


@pytest.mark.parametrize("directory,is_dm", [({"@another:example.org": [ROOM]}, True),
                                            ({MATRIX_USER: ["!elsewhere:example.org"]}, False), ({}, False)])
def test_matrix_admission_reads_the_account_dm_directory(client, transports, monkeypatch, directory, is_dm):
    reader = MatrixClient(MatrixAccount("https://example.org", "@bot:example.org", "test-access"))
    call = MagicMock(return_value=directory)
    monkeypatch.setattr(reader, "_call", call)
    transports["matrix"].is_direct_room = reader.is_direct_room
    assert submit(client, body("matrix"))["status"] == ("ignored" if is_dm else "accepted")
    call.assert_called_once_with("GET", "/_matrix/client/v3/user/%40bot%3Aexample.org/account_data/m.direct")
    assert bool(transports["matrix"].history_calls) is (not is_dm)


@pytest.mark.parametrize("directory", [None, [], {MATRIX_USER: ROOM}, {MATRIX_USER: [123]}])
def test_unreadable_dm_classification_never_fetches_private_history(client, transports, monkeypatch, directory):
    reader = MatrixClient(MatrixAccount("https://example.org", "@bot:example.org", "test-access"))
    monkeypatch.setattr(reader, "_call", MagicMock(return_value=directory))
    transports["matrix"].is_direct_room = reader.is_direct_room
    reply = client.post("/api/assistant/requests", json=body("matrix"), headers=AUTH)
    assert reply.status_code == 502
    assert transports["matrix"].history_calls == []
    assert assistant.read_state().conversation_seen == ()


@pytest.mark.parametrize("status,expected", [(404, 200), (403, 502), (503, 502)])
def test_matrix_dm_directory_read_failures_are_not_empty_directories(client, transports, monkeypatch, status, expected):
    reader = MatrixClient(MatrixAccount("https://example.org", "@bot:example.org", "test-access"))
    monkeypatch.setattr(reader, "_call", MagicMock(side_effect=MatrixError("directory unavailable", status=status)))
    transports["matrix"].is_direct_room = reader.is_direct_room
    reply = client.post("/api/assistant/requests", json=body("matrix"), headers=AUTH)
    assert reply.status_code == expected
    assert bool(transports["matrix"].history_calls) is (status == 404)


def test_unreadable_host_dm_facts_refuse_admission(client, transports):
    directory = Path(dm_parents.REGISTRY_DIR)
    directory.mkdir()
    (directory / "damaged.json").write_text("{")
    reply = client.post("/api/assistant/requests", json=body("matrix"), headers=AUTH)
    assert reply.status_code == 503
    assert transports["matrix"].history_calls == []
    assert assistant.read_state().conversation_seen == ()
