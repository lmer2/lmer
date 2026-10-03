"""Acceptance reactions use real bus records and transport message identities."""
import json
from types import SimpleNamespace

import pytest

from lmer_cli import comms, connector_receipts
from ask_channel import bus
from lmer_cli.connector_delivery import checkpoint_path
from matrix_chat.permalink import matrix_to_link
from matrix_chat.client import MatrixAccount, MatrixClient
from slack_chat.permalink import slack_thread_link
from tests.connector_fakes import MatrixRig, SlackRig, isolated_connectors


@pytest.fixture(params=["slack", "matrix"])
def receipts(request, tmp_path):
    channel = request.param
    rig = (SlackRig if channel == "slack" else MatrixRig)(tmp_path)
    rig.connector._bus_poll_seconds = 0
    rig.connector._inbound_once()
    rig.now = [1000.0]
    rig.connector._receipts.clock = lambda: rig.now[0]
    rig.remote = rig.slack if channel == "slack" else rig.matrix
    yield channel, rig
    assert rig.connector.stop(timeout=30, announce=False)
    rig.bus._session._http.close()
    rig.log.close()


def receive(channel, rig, text="hello"):
    item = rig.remote.human_says(text)
    rig.connector._inbound_once()
    return item if channel == "slack" else item["event_id"]


def test_reaction_after_grace_is_on_the_human_message_without_a_bubble(receipts):
    channel, rig = receipts
    ref = receive(channel, rig)
    rig.now[0] += 19.9
    rig.connector._receipts.step()
    assert rig.remote.reactions == []
    rig.now[0] += 0.1
    rig.connector._receipts.step()
    rig.connector._receipts.step()
    assert len(rig.remote.reactions) == 1
    assert rig.remote.reactions[0][1:3] == (ref, "inbox_tray" if channel == "slack" else "📥")
    assert (rig.slack.posted if channel == "slack" else rig.matrix.sent) == []
    assert not rig.connector._receipt_pending


@pytest.mark.parametrize("kind", ["message", "question", "milestone"])
def test_prompt_agent_reply_cancels_the_receipt(receipts, kind):
    channel, rig = receipts
    receive(channel, rig)
    rig.agent(kind, "reply")
    rig.now[0] += 20
    rig.connector._receipts.step()
    assert rig.remote.reactions == []
    assert not rig.connector._receipt_pending


@pytest.mark.parametrize("local_sender", [True, False])
def test_assistant_receipts_distinguish_own_reply_from_external_steering(receipts, local_sender):
    channel, rig = receipts
    receive(channel, rig)
    rig.log.agent_channel = "uber-lmer"
    record, _ = rig.log.append(
        kind="message", text="reply or steering",
        source=comms.Source("agent" if local_sender else "uber-lmer"),
        session_caller=local_sender,
    )
    assert record.source.channel == "uber-lmer"
    assert bool(record.meta.get("session_origin")) == local_sender
    rig.now[0] += 20
    rig.connector._receipts.step()
    assert len(rig.remote.reactions) == (0 if local_sender else 1)


@pytest.mark.parametrize("kind,meta", [("note", {}), ("message", {"thread": "elsewhere"})])
def test_internal_or_other_thread_posts_do_not_cancel(receipts, kind, meta):
    channel, rig = receipts
    receive(channel, rig)
    rig.agent(kind, "not a reply here", meta=meta)
    rig.now[0] += 20
    rig.connector._receipts.step()
    assert len(rig.remote.reactions) == 1


def test_a_reply_only_cancels_messages_that_precede_it(receipts):
    channel, rig = receipts
    receive(channel, rig, "first")
    receive(channel, rig, "second")
    rig.agent("message", "answer to those")
    ref = receive(channel, rig, "new question")
    rig.now[0] += 20
    rig.connector._receipts.step()
    assert [r[1] for r in rig.remote.reactions] == [ref]


@pytest.mark.parametrize("destination", ["same", "other-root", "other-conversation", "other-transport"])
def test_targeted_reply_cancels_only_the_receiving_thread(receipts, destination):
    channel, rig = receipts
    receive(channel, rig)
    key = rig.connector._lease.key
    conversation, root = key.conversation, key.root
    if destination == "other-conversation":
        conversation = "COTHER" if channel == "slack" else "!other:example.org"
    if destination == "other-root":
        root = "1700000000.999999" if channel == "slack" else "$other"
    if destination == "other-transport":
        target = (matrix_to_link("!room:example.org", "$root") if channel == "slack"
                  else slack_thread_link("example.slack.com", "C1", "1700000000.000000"))
    else:
        target = (slack_thread_link("example.slack.com", conversation, root) if channel == "slack"
                  else matrix_to_link(conversation, root))
    rig.agent("message", "delivered answer", meta={"thread": target})
    rig.now[0] += 20
    rig.connector._receipts.step()
    assert len(rig.remote.reactions) == (0 if destination == "same" else 1)


def test_reply_during_slow_send_cancels_a_later_pending_receipt(receipts, monkeypatch):
    channel, rig = receipts
    first = receive(channel, rig, "first")
    rig.now[0] += 10
    receive(channel, rig, "second")
    rig.now[0] += 10
    send = rig.connector._send_receipt

    def slow_send(ref, transaction):
        send(ref, transaction)
        rig.now[0] += 5
        rig.agent("message", "answer before the second grace period expires")
        rig.now[0] += 6

    monkeypatch.setattr(rig.connector, "_send_receipt", slow_send)
    rig.connector._receipts.step()
    assert [reaction[1] for reaction in rig.remote.reactions] == [first]
    assert not rig.connector._receipt_pending


def test_replay_and_repeated_inbound_polls_add_no_receipts(receipts):
    channel, rig = receipts
    assert not rig.connector._receipt_pending
    ref = receive(channel, rig)
    pending = dict(rig.connector._receipt_pending)
    rig.connector._inbound_once()
    assert rig.connector._receipt_pending == pending
    rig.now[0] += 20
    rig.connector._receipts.step()
    rig.connector._inbound_once()
    assert [r[1] for r in rig.remote.reactions] == [ref]
    assert not rig.connector._receipt_pending


def test_pending_receipt_survives_reload_and_keeps_its_deadline(receipts):
    channel, rig = receipts
    ref = receive(channel, rig)
    old = rig.connector
    assert old.stop(timeout=30, announce=False)
    remote = {channel: rig.remote}
    rig.connector = type(old)(old.config, bus_client=rig.bus, **remote)
    rig.connector._ensure_ready()
    rig.now[0] += 20
    rig.connector._receipts.clock = lambda: rig.now[0]
    rig.connector._receipts.step()
    assert [r[1] for r in rig.remote.reactions] == [ref]


def test_bus_refusal_does_not_schedule_a_receipt(receipts, monkeypatch):
    channel, rig = receipts
    rig.remote.human_says("not accepted yet")
    original = rig.bus.post
    def refuse(*args, **kwargs):
        raise bus.BusError("not accepted")
    monkeypatch.setattr(rig.bus, "post", refuse)
    with pytest.raises(bus.BusError, match="not accepted"):
        rig.connector._inbound_once()
    rig.now[0] += 20
    rig.connector._receipts.step()
    assert rig.remote.reactions == []
    assert not rig.connector._receipt_pending
    monkeypatch.setattr(rig.bus, "post", original)
    rig.connector._inbound_once()
    assert len(rig.connector._receipt_pending) == 1


def test_other_session_bot_input_has_no_human_receipt(receipts):
    channel, rig = receipts
    poster = SimpleNamespace(author="other session")
    if channel == "slack":
        item = {"ts": "1700001111.123456", "user": "UBOT", "bot_id": "B1", "text": "other agent"}
    else:
        item = {"event_id": "$other", "sender": rig.connector.config.user_id,
                "type": "m.room.message", "content": {"body": "other agent", "msgtype": "m.text"}}
    rig.connector._post_inbound(item, poster)
    assert rig.log.read_since(0)[0][-1].text == "other agent"
    rig.now[0] += 20
    rig.connector._receipts.step()
    assert rig.remote.reactions == []


def test_pre_receipt_checkpoints_still_restore(receipts):
    _, rig = receipts
    old = rig.connector
    path = checkpoint_path(old._lease.key, old._state.session)
    assert old.stop(timeout=30, announce=False)
    saved = json.loads(path.read_text())
    saved["transport"].pop("_receipt_pending")
    path.write_text(json.dumps(saved))
    remote = {"slack": rig.remote} if isinstance(rig, SlackRig) else {"matrix": rig.remote}
    rig.connector = type(old)(old.config, bus_client=rig.bus, **remote)
    rig.connector._ensure_ready()
    assert rig.connector._receipt_pending == {}


def test_reaction_failure_does_not_block_delivery_or_post_a_bubble(receipts, monkeypatch, caplog):
    channel, rig = receipts
    receive(channel, rig)
    def refused(*args, **kwargs):
        raise RuntimeError("reaction permission unavailable")
    monkeypatch.setattr(rig.remote, "add_reaction" if channel == "slack" else "send_reaction", refused)
    rig.now[0] += 20
    rig.connector._receipts.step()
    assert "connector_receipt_failed" in caplog.text
    rig.agent("message", "answer still delivered")
    rig.connector._outbound_once()
    posts = rig.slack.posted if channel == "slack" else rig.matrix.sent
    assert [post[2] for post in posts] == ["answer still delivered"]


def test_running_connector_emits_receipt_without_another_model_turn(receipts, monkeypatch):
    channel, rig = receipts
    monkeypatch.setattr(connector_receipts, "GRACE_SECONDS", 0.01)
    monkeypatch.setattr(connector_receipts, "POLL_SECONDS", 0.01)
    rig.connector._receipts.clock = connector_receipts.time.time
    rig.connector._bus_poll_seconds = 0.01
    rig.connector.start()
    rig.remote.human_says("while the model is busy")
    rig.wait_until(lambda: len(rig.remote.reactions) == 1)
    assert (rig.slack.posted if channel == "slack" else rig.matrix.sent) == []


def test_matrix_reaction_request_uses_annotation_and_stable_transaction(monkeypatch):
    client = MatrixClient(MatrixAccount("https://matrix.example.org", "@bot:example.org", "fixture"))
    calls = []
    def request(*args, **kwargs):
        calls.append((args, kwargs))
        return {"event_id": "$reaction"}
    monkeypatch.setattr(client, "_call", request)
    try:
        assert client.send_reaction("!room:example.org", "$message", "📥", transaction="receipt-1") == "$reaction"
        assert calls == [(("PUT", "/_matrix/client/v3/rooms/%21room%3Aexample.org/send/m.reaction/receipt-1"), {
            "json_body": {"m.relates_to": {"rel_type": "m.annotation", "event_id": "$message", "key": "📥"}},
        })]
    finally:
        client._session.close()
