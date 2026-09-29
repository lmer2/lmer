"""Failure and restart at Slack acknowledgement and Matrix cursor boundaries."""
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_sdk.socket_mode.request import SocketModeRequest

from lmer_platform import assistant, ingress, mention_routing, project_requests, projects, project_threads, store
from tests.test_assistant_mentions import isolated, transports, slack_event, matrix_event, ROOM  # noqa: F401
from tests.test_daemon_ingress import fake_socket, manager, until  # noqa: F401
from lmer_platform import ingress_store
from slack_chat.client import SlackError
from tests.test_assistant_mentions import SLACK_USER


@pytest.fixture
def project_inbox(isolated):
    projects.migrate()
    project_threads.choose_unbound("https://example.slack.com/archives/C0123ABCD/p1700000002000000", "general")
    return projects.get("general")


@pytest.mark.asyncio
async def test_slack_journals_before_ack_then_replays_failed_admission_after_restart(monkeypatch, fake_socket, project_inbox):
    component = manager(monkeypatch)
    original = mention_routing.receive
    def failed(*_args, **_kwargs):
        raise OSError("Fixture admission unavailable")
    monkeypatch.setattr(mention_routing, "receive", failed)
    at_ack = []
    async def acknowledge(_response):
        path = store.platform_dir() / "ingress.sqlite3"
        with sqlite3.connect(path) as db:
            at_ack.extend(db.execute("SELECT conversation,event FROM inbox WHERE event IS NOT NULL").fetchall())
    socket = SimpleNamespace(send_socket_mode_response=AsyncMock(side_effect=acknowledge))
    request = SocketModeRequest(type="events_api", envelope_id="durable", payload={"event": slack_event()})
    await component._slack_event(socket, request)
    assert len(at_ack) == 1, "Slack acknowledged before durably retaining the raw event"
    assert project_requests.listing() == []
    monkeypatch.setattr(mention_routing, "receive", original)
    restarted = manager(monkeypatch)
    async with restarted.lifespan(None):
        await until(lambda: len(project_requests.listing()) == 1)
        await fake_socket[0][0].emit(slack_event())
        assert len(project_requests.listing()) == 1
        assert len(assistant.read_state().pending) == 1
    assert not restarted._tasks


@pytest.mark.asyncio
async def test_slack_does_not_ack_when_durable_write_fails(monkeypatch, transports):
    original = sqlite3.connect
    def unavailable(path, *args, **kwargs):
        if str(path).endswith("ingress.sqlite3"):
            raise sqlite3.OperationalError("Fixture disk unavailable")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", unavailable)
    reader = transports["slack"]
    reader.session = MagicMock()
    monkeypatch.setattr(ingress, "SlackClient", lambda *a, **kw: reader)
    socket = SimpleNamespace(send_socket_mode_response=AsyncMock())
    component = manager(monkeypatch)
    request = SocketModeRequest(type="events_api", envelope_id="retry", payload={"event": slack_event()})
    await component._slack_event(socket, request)
    socket.send_socket_mode_response.assert_not_awaited()
    assert not assistant.read_state().pending


class Disconnect(Exception):
    pass


@pytest.mark.asyncio
async def test_matrix_persists_batch_with_cursor_and_replays_on_new_ingress(monkeypatch, transports, project_inbox):
    reader = transports["matrix"]
    reader._session = MagicMock()
    reader.whoami = lambda: "@bot:example.org"
    reader.join = lambda _room: ROOM
    calls = []
    def sync(**kwargs):
        calls.append(kwargs["since"])
        if len(calls) == 3:
            raise Disconnect()
        event = matrix_event(event_id="$old" if len(calls) == 1 else "$retained")
        return {"next_batch": str(len(calls)), "rooms": {"join": {ROOM: {"timeline": {"events": [event]}}}}}
    reader.sync = sync
    monkeypatch.setattr(ingress, "MatrixClient", lambda *a, **kw: reader)
    original = mention_routing.receive
    def failed(*_args, **_kwargs):
        raise OSError("Fixture admission unavailable")
    monkeypatch.setattr(mention_routing, "receive", failed)
    component = manager(monkeypatch, matrix=True)
    component.env["LMER_MATRIX_ROOMS"] = ROOM
    with pytest.raises(Disconnect):
        await component._matrix()
    assert component._matrix_since == "2"
    assert project_requests.listing() == []
    monkeypatch.setattr(mention_routing, "receive", original)
    resumed = []
    def disconnected(**kwargs):
        resumed.append(kwargs["since"])
        raise Disconnect()
    reader.sync = disconnected
    restarted = manager(monkeypatch, matrix=True)
    restarted.env["LMER_MATRIX_ROOMS"] = ROOM
    with pytest.raises(Disconnect):
        await restarted._matrix()
    assert resumed == ["2"], "The acknowledged Matrix cursor was lost on restart"
    requests = project_requests.listing()
    assert len(requests) == 1
    assert requests[0]["state"] == "queued"
    assert requests[0]["project_id"] == "general"
    assert len(reader.posts) == 1
    with pytest.raises(Disconnect):
        await restarted._matrix()
    assert len(reader.posts) == 1


@pytest.mark.asyncio
async def test_committed_admission_is_idempotent_when_processing_response_is_lost(monkeypatch, fake_socket, project_inbox):
    original = mention_routing.receive
    calls = []
    def lost_response(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result)
        if len(calls) == 1:
            raise OSError("Fixture response lost after commit")
        return result
    monkeypatch.setattr(mention_routing, "receive", lost_response)
    component = manager(monkeypatch)
    async with component.lifespan(None):
        await until(lambda: component.snapshot()["slack"]["up"])
        await fake_socket[0][0].emit(slack_event())
        await until(lambda: len(calls) >= 2)
        assert len(project_requests.listing()) == 1
        assert len(assistant.read_state().pending) == 1
        assert calls[1]["status"] == "duplicate"


@pytest.mark.asyncio
async def test_matrix_cursor_rolls_back_if_one_event_in_batch_cannot_be_persisted(monkeypatch, transports):
    reader = transports["matrix"]
    reader._session = MagicMock()
    reader.whoami = lambda: "@bot:example.org"
    responses = iter([
        {"next_batch": "baseline"},
        {"next_batch": "uncommitted", "rooms": {"join": {ROOM: {"timeline": {"events": [
            matrix_event(event_id="$one"), matrix_event(event_id="$two"),
        ]}}}}},
    ])
    reader.sync = lambda **_kwargs: next(responses)
    monkeypatch.setattr(ingress, "MatrixClient", lambda *a, **kw: reader)
    original = ingress_store._insert
    def fail_second(db, transport, conversation, event):
        if event.get("event_id") == "$two":
            raise OSError("Fixture second insert failure")
        return original(db, transport, conversation, event)
    monkeypatch.setattr(ingress_store, "_insert", fail_second)
    component = manager(monkeypatch, matrix=True)
    with pytest.raises(OSError, match="second insert"):
        await component._matrix()
    assert component._matrix_since == "baseline"
    assert ingress_store.cursor("https://example.org\n@bot:example.org")["since"] == "baseline"
    assert ingress_store.pending("matrix") == []


@pytest.mark.asyncio
async def test_choice_notice_replays_after_request_commit_before_notice(monkeypatch, fake_socket):
    projects.migrate()
    projects.create("Example")
    projects.create("Example Alpha")
    original = project_requests._notice
    calls = []
    def interrupted(*args, **kwargs):
        calls.append(None)
        if len(calls) == 1:
            raise OSError("Fixture stop before notice begins")
        return original(*args, **kwargs)
    monkeypatch.setattr(project_requests, "_notice", interrupted)
    component = manager(monkeypatch)
    async with component.lifespan(None):
        await until(lambda: component.snapshot()["slack"]["up"])
        await fake_socket[0][0].emit(slack_event(text="Example Alpha hi"))
        await until(lambda: project_requests.listing()[0]["prompt_state"] == "sent")
        assert len(calls) == 2
        row = project_requests.listing()[0]
        assert row["state"] == "choice"
        assert row["prompt_state"] == "sent"


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup", ["success", "unavailable", "denied", "id", "denied_id", "empty"])
async def test_slack_authorization_lookup_retains_only_unavailable_events(monkeypatch, transports, lookup):
    projects.migrate()
    monkeypatch.setenv("LMER_SLACK_DM_ALLOWED_USERS", {
        "id": SLACK_USER, "denied_id": "UOTHER999", "empty": "", "denied": "someone-else",
    }.get(lookup, "operator"))
    reader = transports["slack"]
    reader.session = MagicMock()
    monkeypatch.setattr(ingress, "SlackClient", lambda *a, **kw: reader)
    original = reader._request
    calls = []
    def identify(*args, **kwargs):
        calls.append(args[1])
        if lookup in ("unavailable", "id", "denied_id", "empty"):
            raise SlackError("Fixture temporary lookup failure", code="ratelimited")
        return original(*args, **kwargs)
    monkeypatch.setattr(reader, "_request", identify)
    at_ack = []
    async def acknowledge(_response):
        at_ack.append(len(ingress_store.pending("slack")))
    socket = SimpleNamespace(send_socket_mode_response=AsyncMock(side_effect=acknowledge))
    event = SocketModeRequest(type="events_api", envelope_id="lookup", payload={"event": slack_event()})
    component = manager(monkeypatch)
    await component._slack_event(socket, event)
    assert at_ack == [1]
    assert calls == ([] if lookup in ("id", "denied_id", "empty") else ["users.info"])
    assert len(project_requests.listing()) == (1 if lookup in ("success", "id") else 0)
    retained = ingress_store.pending("slack")
    assert len(retained) == (1 if lookup == "unavailable" else 0)
    if lookup == "unavailable":
        assert component.snapshot()["slack"]["last_error"] == "ratelimited"
        assert retained[0]["event"] == slack_event()
    assert len(reader.posts) == (1 if lookup in ("success", "id") else 0)
    monkeypatch.setattr(reader, "_request", original)
    restarted = manager(monkeypatch)
    await restarted._replay("slack", reader)
    await restarted._slack_event(socket, event)
    assert len(project_requests.listing()) == (0 if lookup in ("denied", "denied_id", "empty") else 1)
    assert len(reader.posts) == (0 if lookup in ("denied", "denied_id", "empty") else 1)
    assert ingress_store.pending("slack") == []
