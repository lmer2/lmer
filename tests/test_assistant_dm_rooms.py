"""DM send provenance remains authoritative when Matrix account data is lost."""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from lmer_cli import dm_parents, thread_access
from lmer_cli.threads import ThreadError
from lmer_platform import api
from lmer_platform import config as cfg
from matrix_chat.client import MatrixAccount, MatrixClient, MatrixError
from tests.test_assistant_mentions import (
    MATRIX_USER, ROOM, SECRET, body, client, isolated, matrix_event, pending,
    submit, transports,
)
from tests.test_assistant_mentions_review import dispatch, through_http


def reader_with_lost_directory():
    reader = MatrixClient(MatrixAccount("https://example.org", "@bot:example.org", "test-access"))

    def call(method, path, **kwargs):
        if path.endswith("/account_data/m.direct"):
            if method == "GET":
                return {}
            raise MatrixError("directory update unavailable", status=503)
        if path.endswith("/createRoom"):
            assert kwargs["json_body"]["is_direct"] is True
            return {"room_id": ROOM}
        if path.endswith("/state/m.room.encryption"):
            raise MatrixError("not encrypted", status=404)
        if "/send/m.room.message/" in path:
            return {"event_id": "$sent-dm"}
        raise AssertionError((method, path))

    reader._call = MagicMock(side_effect=call)
    reader.history_before = MagicMock(return_value=[matrix_event(text="private DM history")])
    return reader


def servicer(kind):
    result = thread_access.ThreadServicer(
        supervisor_url="http://127.0.0.1:1", token=SECRET, env=dict(os.environ),
        assistant_session=kind == "assistant",
    )
    result._session = f"round2-{kind}"
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["worker", "assistant"])
@pytest.mark.parametrize("relation", ["top-level", "sent-parent", "other-parent"])
async def test_successful_dm_survives_lost_directory_without_granting_worker_parents(
    client, monkeypatch, transports, kind, relation, caplog,
):
    sender = reader_with_lost_directory()
    monkeypatch.setattr(thread_access, "_matrix_client", lambda account: sender)
    sent = servicer(kind).handle({"op": "dm", "recipient": MATRIX_USER, "text": "private DM"})
    assert sent["ok"]
    assert "matrix_direct_write_failed" in caplog.text
    assert (dm_parents.lookup("matrix", ROOM, "$sent-dm") is not None) is (kind == "assistant")

    reader = reader_with_lost_directory()
    assert reader.is_direct_room(ROOM) is False
    monkeypatch.setattr(thread_access, "_matrix_client", lambda account: reader)
    transports["matrix"] = reader
    daemon = TestClient(api.create_app(cfg.load(), SECRET))
    through_http(monkeypatch, daemon)
    request = body("matrix", dm=True, mention=True)
    if relation != "top-level":
        root = "$sent-dm" if relation == "sent-parent" else "$other"
        request["event"]["content"]["m.relates_to"] = {"rel_type": "m.thread", "event_id": root}
    await dispatch(monkeypatch, transports, "matrix", request)
    notes = pending(daemon)
    eligible = kind == "assistant" and relation == "sent-parent"
    assert len(notes) == int(eligible)
    reader.history_before.assert_not_called()
    assert submit(daemon, request)["status"] == ("duplicate" if eligible else "ignored")
    assert dm_parents.known_conversation("matrix", ROOM)
    if eligible:
        assert notes[0]["data"]["entry"] == "dm_reply"
        assert notes[0]["data"]["parent"]["session"] == "round2-assistant"


@pytest.mark.parametrize("existing", [False, True])
def test_dm_room_is_recorded_before_send_even_with_routing_disabled(monkeypatch, existing):
    monkeypatch.setenv("LMER_ASSISTANT_MENTION_ROUTING", "false")
    reader = reader_with_lost_directory()
    if existing:
        reader.direct_room = lambda recipient: ROOM
    send = reader.send_message

    def send_after_record(room, content):
        assert dm_parents.known_conversation("matrix", room)
        assert dm_parents.lookup("matrix", room, "$sent-dm") is None
        return send(room, content)

    reader.send_message = send_after_record
    monkeypatch.setattr(thread_access, "_matrix_client", lambda account: reader)
    assert servicer("worker").handle({"op": "dm", "recipient": MATRIX_USER, "text": "private"})["ok"]


@pytest.mark.parametrize("kind", ["worker", "assistant"])
def test_dm_registry_failure_refuses_the_message(monkeypatch, tmp_path, kind):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("cannot store room identity here")
    monkeypatch.setattr(dm_parents, "REGISTRY_DIR", blocked)
    reader = reader_with_lost_directory()
    reader.send_message = MagicMock()
    monkeypatch.setattr(thread_access, "_matrix_client", lambda account: reader)
    with pytest.raises(ThreadError, match="no direct message was sent") as caught:
        servicer(kind).handle({"op": "dm", "recipient": MATRIX_USER, "text": "private"})
    assert caught.value.status == 503
    reader.send_message.assert_not_called()


def test_failed_message_send_keeps_the_room_private(monkeypatch):
    reader = reader_with_lost_directory()
    reader.send_message = MagicMock(side_effect=MatrixError("send unavailable", status=503))
    monkeypatch.setattr(thread_access, "_matrix_client", lambda account: reader)
    with pytest.raises(ThreadError, match="send unavailable"):
        servicer("worker").handle({"op": "dm", "recipient": MATRIX_USER, "text": "private"})
    assert dm_parents.known_conversation("matrix", ROOM)
    assert dm_parents.lookup("matrix", ROOM, "$sent-dm") is None
