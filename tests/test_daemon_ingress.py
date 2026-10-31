"""Ingress lifecycle and transport callbacks, through the real admission path."""
from __future__ import annotations

import asyncio
import contextvars
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from lmer_cli import assistant_mentions
from lmer_platform import api, assistant, ingress, ingress_store, mention_routing
from lmer_platform import config as cfg
from matrix_chat.client import MatrixError
from slack_chat.client import SlackError
from slack_sdk.socket_mode.request import SocketModeRequest
from tests.test_assistant_mentions import (
    AUTH, ROOM, SECRET,
    body, isolated, matrix_event, remember, slack_event, transports,
)
from lmer_platform import daemon, project_requests
from tests.timing_budgets import JOIN


def manager(monkeypatch, *, matrix=False):
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    component = ingress.Ingress()
    if not matrix:
        for key in ("LMER_MATRIX_HOMESERVER", "LMER_MATRIX_USER_ID", "LMER_MATRIX_ACCESS_TOKEN"):
            component.env.pop(key)
        component._states["matrix"]["state"] = "unconfigured"
    return component


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.005)
    # JOIN, not 5 s: a loaded gate missed a 5 s wait here (#636).
    await asyncio.wait_for(wait(), timeout=JOIN)


def test_configuration_is_visible_without_network():
    disabled = ingress.Ingress(env={})
    assert {v["state"] for v in disabled.snapshot().values()} == {"disabled"}
    partial = ingress.Ingress(env={assistant_mentions.ENABLED_ENV: "true", "SLACK_BOT_TOKEN": "test"})
    assert partial.snapshot()["slack"]["last_error"] == "missing SLACK_APP_TOKEN"
    assert partial.snapshot()["slack"]["up"] is False
    assert partial.snapshot()["matrix"]["state"] == "unconfigured"
    snapshot = partial.snapshot()
    snapshot["slack"]["last_error"] = "changed"
    assert partial.snapshot()["slack"]["last_error"] == "missing SLACK_APP_TOKEN"


@pytest.mark.asyncio
async def test_disabled_and_partial_configuration_never_start_transport(monkeypatch):
    for env in ({}, {assistant_mentions.ENABLED_ENV: "true", "SLACK_BOT_TOKEN": "test"}):
        component = ingress.Ingress(env=env)
        run = AsyncMock()
        monkeypatch.setattr(component, "_supervise", run)
        async with component.lifespan(None):
            assert not any(row["up"] for row in component.snapshot().values())
        run.assert_not_called()


@pytest.fixture
def fake_socket(monkeypatch, transports):
    instances = []
    webs = []
    reader = transports["slack"]
    reader.session = MagicMock()
    monkeypatch.setattr(ingress, "SlackClient", lambda *a, **kw: reader)

    class Web:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.auth_test = AsyncMock()
            webs.append(self)

    class Socket:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.socket_mode_request_listeners = []
            self.send_socket_mode_response = AsyncMock()
            self.connected = False
            self.closed = False
            instances.append(self)

        async def issue_new_wss_url(self):
            return "wss://example.invalid/socket"

        async def connect(self):
            self.connected = True

        async def is_connected(self):
            return self.connected

        async def close(self):
            self.closed = True
            self.connected = False

        async def emit(self, event):
            request = SocketModeRequest(type="events_api", envelope_id="envelope", payload={"event": event})
            assert len(self.socket_mode_request_listeners) == 1
            await self.socket_mode_request_listeners[0](self, request)

    monkeypatch.setattr("slack_sdk.web.async_client.AsyncWebClient", Web)
    monkeypatch.setattr("slack_sdk.socket_mode.aiohttp.SocketModeClient", Socket)
    return instances, webs


@pytest.mark.asyncio
async def test_daemon_socket_callback_admits_and_external_route_dedupes(monkeypatch, fake_socket):
    component = manager(monkeypatch)
    app = api.create_app(cfg.load(), SECRET, ingress=component)
    sockets, webs = fake_socket
    async with app.router.lifespan_context(app):
        await until(lambda: component.snapshot()["slack"]["up"])
        assert sockets[0].kwargs["app_token"] == "xapp-test"
        webs[0].auth_test.assert_awaited_once()
        await sockets[0].emit(slack_event())
        sockets[0].send_socket_mode_response.assert_awaited_once()
        pending = assistant.read_state().pending
        assert len(pending) == 1
        assert pending[0].kind == "conversation_request"
        client = TestClient(app)
        assert client.post("/api/assistant/requests", headers=AUTH, json=body("slack")).json()["status"] == "duplicate"
        health = client.get("/api/health", headers=AUTH).json()["ingress"]["slack"]
        assert health["up"] is True
        assert health["last_event_at"]
    assert sockets[0].closed
    assert component.snapshot()["slack"]["state"] == "stopped"
    assert not component._tasks


@pytest.mark.asyncio
async def test_sdk_disconnect_is_reported_and_recovery_is_visible(monkeypatch, fake_socket):
    component = manager(monkeypatch)
    async with component.lifespan(None):
        await until(lambda: component.snapshot()["slack"]["up"])
        fake_socket[0][0].connected = False
        await until(lambda: component.snapshot()["slack"]["state"] == "reconnecting")
        assert component.snapshot()["slack"]["last_error"] == "disconnected"
        fake_socket[0][0].connected = True
        await until(lambda: component.snapshot()["slack"]["up"])
        assert component.snapshot()["slack"]["last_error"] == "disconnected"


@pytest.mark.asyncio
async def test_transport_failure_retries_independently_and_shutdown_cancels(monkeypatch):
    component = manager(monkeypatch, matrix=True)
    monkeypatch.setattr(ingress, "RETRY_MAX", 0.01)
    calls = []
    stopped = []
    async def slack():
        calls.append("slack")
        if len(calls) == 1:
            raise SlackError("private payload", code="invalid_auth")
        component._update("slack", up=True, state="up")
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append("slack")
    async def matrix():
        component._update("matrix", up=True, state="up")
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append("matrix")
    monkeypatch.setattr(component, "_slack", slack)
    monkeypatch.setattr(component, "_matrix", matrix)
    async with component.lifespan(None):
        await until(lambda: component.snapshot()["slack"]["up"])
        assert calls == ["slack", "slack"]
        assert component.snapshot()["matrix"]["up"]
        assert component.snapshot()["slack"]["last_error"] == "invalid_auth"
    assert set(stopped) == {"slack", "matrix"}


@pytest.mark.asyncio
async def test_socket_setup_failure_closes_client_and_reports_reason(monkeypatch, fake_socket):
    component = manager(monkeypatch)
    socket_type = __import__("slack_sdk.socket_mode.aiohttp", fromlist=["SocketModeClient"]).SocketModeClient
    monkeypatch.setattr(socket_type, "issue_new_wss_url",
                        AsyncMock(side_effect=SlackError("private", code="invalid_auth")))
    with pytest.raises(SlackError):
        await component._slack()
    assert fake_socket[0][0].closed
    assert component.snapshot()["slack"]["last_error"] == "invalid_auth"
    assert not component.snapshot()["slack"]["up"]


@pytest.mark.asyncio
async def test_real_sdk_websocket_failure_keeps_private_details_out_of_diagnostics(monkeypatch, caplog):
    import ssl
    from slack_sdk.socket_mode.aiohttp import SocketModeClient
    from slack_sdk.web.async_client import AsyncWebClient

    component = manager(monkeypatch)
    monkeypatch.setattr(ingress, "IO_TIMEOUT", 0.05)
    monkeypatch.setattr(AsyncWebClient, "auth_test", AsyncMock())
    monkeypatch.setattr(SocketModeClient, "issue_new_wss_url",
                        AsyncMock(return_value="wss://example.invalid/private-signed-url"))
    connect = AsyncMock(side_effect=ssl.SSLCertVerificationError("private certificate details"))
    monkeypatch.setattr("aiohttp.ClientSession.ws_connect", connect)
    caplog.set_level("DEBUG")
    with pytest.raises(Exception) as caught:
        await component._slack()
    component._failure("slack", caught.value)
    connect.assert_awaited_once()
    assert component.snapshot()["slack"]["last_error"] == "SSLCertVerificationError"
    assert "reason=SSLCertVerificationError" in caplog.text
    assert "private certificate details" not in caplog.text
    assert "private-signed-url" not in caplog.text
    assert "CancelledError" not in caplog.text


@pytest.mark.asyncio
async def test_real_sdk_dispatch_ack_and_close(monkeypatch, transports):
    import aiohttp
    from slack_sdk.socket_mode.aiohttp import SocketModeClient
    from slack_sdk.web.async_client import AsyncWebClient

    component = manager(monkeypatch)
    reader = transports["slack"]
    reader.session = MagicMock()
    monkeypatch.setattr(ingress, "SlackClient", lambda *a, **kw: reader)
    async with aiohttp.ClientSession() as session:
        socket = SocketModeClient(app_token="test", web_client=AsyncWebClient(token="test", session=session))
        socket.send_socket_mode_response = AsyncMock()
        socket.socket_mode_request_listeners.append(component._slack_event)
        try:
            await socket.run_message_listeners({
                "type": "events_api", "envelope_id": "one", "payload": {"event": slack_event()},
            }, "")
            assert len(assistant.read_state().pending) == 1
            assert socket.send_socket_mode_response.await_args.args[0].envelope_id == "one"
        finally:
            await socket.close()
            await asyncio.gather(socket.message_processor, return_exceptions=True)
        assert socket.closed
        assert socket.message_processor.done()


@pytest.mark.asyncio
async def test_shutdown_cancels_sdk_reconnect_dispatch(monkeypatch):
    from slack_sdk.socket_mode.aiohttp import SocketModeClient
    from slack_sdk.web.async_client import AsyncWebClient
    component = manager(monkeypatch)
    sockets = []
    original = SocketModeClient.__init__
    def build(socket, *args, **kwargs):
        original(socket, *args, **kwargs)
        sockets.append(socket)
    monkeypatch.setattr(SocketModeClient, "__init__", build)
    monkeypatch.setattr(AsyncWebClient, "auth_test", AsyncMock())
    monkeypatch.setattr(SocketModeClient, "issue_new_wss_url", AsyncMock(return_value="wss://example.invalid"))
    monkeypatch.setattr(SocketModeClient, "connect", AsyncMock())
    monkeypatch.setattr(SocketModeClient, "is_connected", AsyncMock(return_value=True))
    reconnecting = asyncio.Event()
    async def reconnect(*args, **kwargs):
        reconnecting.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(SocketModeClient, "connect_to_new_endpoint", reconnect)
    task = None
    try:
        async with component.lifespan(None):
            await until(lambda: component.snapshot()["slack"]["up"])
            task = asyncio.create_task(sockets[0].run_message_listeners({"type": "disconnect"}, ""))
            await asyncio.wait_for(reconnecting.wait(), JOIN)
        assert task.done(), "SDK reconnect dispatch survived daemon shutdown"
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("close", ["clean", "raises"])
@pytest.mark.parametrize("owner", ["callback", "loop"])
async def test_shutdown_waits_for_inflight_admission(monkeypatch, fake_socket, owner, close):
    """The socket callback and the supervise loop's 1s replay race for a stored
    event (#524); each case pins one winner. Either way the socket closes before
    the admission is released, and shutdown still waits for it, even when the
    close itself raises (!422 review)."""
    component = manager(monkeypatch)
    entered, release, admitted = threading.Event(), threading.Event(), threading.Event()
    replayer = contextvars.ContextVar("replayer", default=None)
    original = mention_routing.receive
    owners = []
    def receive(*args, **kwargs):
        owners.append(replayer.get())
        entered.set()
        assert release.wait(JOIN), "test did not release admission"
        try:
            return original(*args, **kwargs)
        finally:
            admitted.set()
    monkeypatch.setattr(mention_routing, "receive", receive)
    callback = {}
    replay = component._replay
    async def tagged(transport, client):
        replayer.set("callback" if asyncio.current_task() is callback.get("task") else "loop")
        return await replay(transport, client)
    monkeypatch.setattr(component, "_replay", tagged)
    stored = ingress_store.pending
    def pending(transport):
        # to_thread carries the replayer's context, so the loser sees no row.
        if replayer.get() != owner and not entered.is_set():
            return []
        return stored(transport)
    monkeypatch.setattr(ingress_store, "pending", pending)
    lifetime = component.lifespan(None)
    await lifetime.__aenter__()
    stopping = event = None
    finished_after_admission = []
    try:
        await until(lambda: component.snapshot()["slack"]["up"])
        if close == "raises":
            socket = fake_socket[0][0]
            clean_close = socket.close

            async def failing_close():
                await clean_close()
                raise ConnectionResetError("injected close failure")
            socket.close = failing_close
        event = callback["task"] = asyncio.create_task(fake_socket[0][0].emit(slack_event()))
        await until(entered.is_set)
        stopping = asyncio.create_task(lifetime.__aexit__(None, None, None))
        stopping.add_done_callback(lambda _: finished_after_admission.append(admitted.is_set()))
        await until(lambda: fake_socket[0][0].closed)
        assert not stopping.done()
        release.set()
        await asyncio.wait_for(stopping, JOIN)
        await event
        assert owners == [owner], "the pin did not decide which replay admitted"
        assert finished_after_admission == [True]
        assert fake_socket[0][0].closed
        assert len(assistant.read_state().pending) == 1
        assert not component._events
    finally:
        release.set()
        if stopping is None:
            await lifetime.__aexit__(None, None, None)
        else:
            await asyncio.wait_for(asyncio.gather(stopping, return_exceptions=True), JOIN)
        if event is not None:
            await asyncio.wait_for(asyncio.gather(event, return_exceptions=True), JOIN)


@pytest.mark.asyncio
@pytest.mark.parametrize("dm,matched", [(False, False), (True, False), (True, True)])
async def test_socket_preserves_dm_parent_and_no_backlog(monkeypatch, fake_socket, transports, dm, matched):
    component = manager(monkeypatch)
    request = body("slack", dm=dm, threaded=matched)
    request["event"]["channel"] = request["conversation"]
    if dm:
        request["event"]["channel_type"] = "im"
        request["event"]["type"] = "message"
    if matched:
        remember("slack")
    async with component.lifespan(None):
        await until(lambda: component.snapshot()["slack"]["up"])
        await fake_socket[0][0].emit(request["event"])
        assert len(assistant.read_state().pending) == (0 if dm and not matched else 1)
        if dm:
            assert not transports["slack"].history_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["disabled", "denied", "not_in_channel", "connected"])
async def test_each_dropped_event_logs_one_reason_and_no_body(monkeypatch, transports, caplog, reason):
    component = manager(monkeypatch)
    caplog.set_level("INFO")
    if reason == "disabled":
        component.settings = assistant_mentions.Settings(enabled=False, backlog=2)
    elif reason == "denied":
        component.env["LMER_SLACK_DM_ALLOWED_USERS"] = ""
    elif reason == "not_in_channel":
        def denied(*args):
            raise SlackError("DO NOT LOG THIS", code="not_in_channel")
        transports["slack"].history_before = denied
    elif reason == "connected":
        monkeypatch.setattr(mention_routing, "_held", lambda *a: True)
    await component._receive("slack", "C0123ABCD", slack_event(text="PRIVATE MESSAGE"), transports["slack"])
    records = [r for r in caplog.records if "assistant_ingress_dropped" in r.message]
    assert len(records) == 1
    assert f"reason={reason}" in records[0].message
    assert "PRIVATE MESSAGE" not in caplog.text
    assert "DO NOT LOG THIS" not in caplog.text
    assert not assistant.read_state().pending
    if reason == "not_in_channel":
        assert component.snapshot()["slack"]["last_error"] == reason


@pytest.mark.asyncio
async def test_matrix_sync_skips_initial_history_admits_and_retains_cursor(monkeypatch, transports, caplog):
    component = manager(monkeypatch, matrix=True)
    reader = transports["matrix"]
    reader._session = MagicMock()
    reader.whoami = lambda: "@bot:example.org"
    reader.join = MagicMock(return_value=ROOM)
    component.env["LMER_MATRIX_ROOMS"] = "#room:example.org"
    calls = []
    def sync(**kwargs):
        calls.append(kwargs)
        if len(calls) > 2:
            raise MatrixError("test disconnection", errcode="M_UNAVAILABLE")
        return {"next_batch": str(len(calls)), "rooms": {"join": {ROOM: {
            "timeline": {"events": [matrix_event(event_id="$old" if len(calls) == 1 else "$new")]},
        }}}}
    reader.sync = sync
    monkeypatch.setattr(ingress, "MatrixClient", lambda *a, **kw: reader)
    caplog.set_level("INFO")
    with pytest.raises(MatrixError):
        await component._matrix()
    assert "reason=initial_sync" in caplog.text
    assert len(assistant.read_state().pending) == 1
    assert reader.history_calls == [(ROOM, "$new", 2)]
    assert [c["since"] for c in calls] == [None, "1", "2"]
    assert calls[0]["filter_body"]["room"]["rooms"] == [ROOM]
    assert component._matrix_since == "2"
    assert component.snapshot()["matrix"]["last_event_at"]
    reader._session.close.assert_called_once()
    with pytest.raises(MatrixError):
        await component._matrix()
    assert calls[-1]["since"] == "2", "reconnect must not skip fresh events as initial history"


@pytest.mark.asyncio
@pytest.mark.parametrize("recovers", [False, True])
async def test_matrix_failed_room_join_keeps_syncing_healthy_room(monkeypatch, transports, caplog, recovers):
    component = manager(monkeypatch, matrix=True)
    reader = transports["matrix"]
    reader._session = MagicMock()
    reader.whoami = lambda: "@bot:example.org"
    stale = "!z-stale:example.org"
    component.env["LMER_MATRIX_ROOMS"] = f"{ROOM},{stale}"
    joins = []
    def join(room):
        joins.append(room)
        if room == stale and (not recovers or joins.count(stale) == 1):
            raise MatrixError("private join details", errcode="M_FORBIDDEN")
        return room
    reader.join = join
    syncs = []
    def sync(**kwargs):
        syncs.append({"since": kwargs["since"], "rooms": list(kwargs["filter_body"]["room"]["rooms"])})
        if len(syncs) > 2:
            raise MatrixError("stop test", errcode="M_UNAVAILABLE")
        return {"next_batch": str(len(syncs)), "rooms": {"join": {ROOM: {
            "timeline": {"events": [matrix_event(event_id="$old" if len(syncs) == 1 else "$new")]},
        }}}}
    reader.sync = sync
    monkeypatch.setattr(ingress, "MatrixClient", lambda *a, **kw: reader)
    monkeypatch.setattr(ingress, "RETRY_MAX", 0)
    caplog.set_level("INFO")
    with pytest.raises(MatrixError):
        await component._matrix()
    assert len(syncs) == 3, "an inaccessible room prevented healthy-room sync"
    assert joins.count(ROOM) == 1
    assert joins.count(stale) >= 2
    assert syncs[0]["rooms"] == [ROOM]
    assert syncs[1:] == [
        {"since": cursor, "rooms": [ROOM, stale] if recovers else [ROOM]} for cursor in ("1", "2")
    ]
    assert len(assistant.read_state().pending) == 1
    assert component.snapshot()["matrix"]["up"]
    assert component.snapshot()["matrix"]["last_error"] == "M_FORBIDDEN"
    assert f"conversation={stale} reason=M_FORBIDDEN" in caplog.text
    assert "private join details" not in caplog.text
    reader._session.close.assert_called_once()


@pytest.mark.asyncio
async def test_matrix_no_selected_room_joined_waits_and_cancels_without_unfiltered_sync(monkeypatch, transports):
    component = manager(monkeypatch, matrix=True)
    component.env["LMER_MATRIX_ROOMS"] = "#selected:example.org"
    reader = transports["matrix"]
    reader._session = MagicMock()
    reader.whoami = lambda: "@bot:example.org"
    reader.join = MagicMock(side_effect=MatrixError("private", errcode="M_FORBIDDEN"))
    reader.sync = MagicMock()
    monkeypatch.setattr(ingress, "MatrixClient", lambda *a, **kw: reader)
    monkeypatch.setattr(ingress, "RETRY_MAX", 0.01)
    task = asyncio.create_task(component._matrix())
    try:
        await until(lambda: reader.join.call_count >= 2)
        reader.sync.assert_not_called()
        assert component.snapshot()["matrix"]["up"] is False
        assert component.snapshot()["matrix"]["state"] == "retrying"
        assert component.snapshot()["matrix"]["last_error"] == "M_FORBIDDEN"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    reader._session.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [False, True])
async def test_matrix_unset_selection_and_recovery_from_no_joined_selection(monkeypatch, transports, selected):
    component = manager(monkeypatch, matrix=True)
    if selected:
        component.env["LMER_MATRIX_ROOMS"] = "#selected:example.org"
    reader = transports["matrix"]
    reader._session = MagicMock()
    reader.whoami = lambda: "@bot:example.org"
    reader.join = MagicMock(side_effect=[MatrixError("private", errcode="M_FORBIDDEN"), ROOM])
    syncs = []
    def sync(**kwargs):
        syncs.append(kwargs)
        if selected:
            assert reader.join.call_count == 2
            assert kwargs["filter_body"]["room"]["rooms"] == [ROOM]
        else:
            reader.join.assert_not_called()
            assert "rooms" not in kwargs["filter_body"]["room"]
        if len(syncs) > 2:
            raise MatrixError("stop test", errcode="M_UNAVAILABLE")
        return {"next_batch": str(len(syncs)), "rooms": {"join": {ROOM: {
            "timeline": {"events": [matrix_event(event_id="$old" if len(syncs) == 1 else "$new")]},
        }}}}
    reader.sync = sync
    monkeypatch.setattr(ingress, "MatrixClient", lambda *a, **kw: reader)
    monkeypatch.setattr(ingress, "RETRY_MAX", 0)
    with pytest.raises(MatrixError):
        await component._matrix()
    assert [call["since"] for call in syncs] == [None, "1", "2"]
    assert len(assistant.read_state().pending) == 1
    assert component.snapshot()["matrix"]["up"]
    reader._session.close.assert_called_once()


@pytest.mark.asyncio
async def test_matrix_join_recovery_discards_room_history_and_keeps_live_cursor(monkeypatch, transports, caplog):
    component = manager(monkeypatch, matrix=True)
    late = "!late:example.org"
    component.env["LMER_MATRIX_ROOMS"] = f"{ROOM},{late}"
    reader = transports["matrix"]
    reader._session = MagicMock()
    reader.whoami = lambda: "@bot:example.org"
    joined = []
    def join(room):
        joined.append(room)
        if room == late and joined.count(late) == 1:
            raise MatrixError("not invited yet", errcode="M_FORBIDDEN")
        return room
    reader.join = join
    batches = {
        1: {ROOM: "$old"},
        2: {ROOM: "$good", late: "$old-late"},
        3: {late: "$late-live"},
        5: {ROOM: "$after-reconnect", late: "$late-after-reconnect"},
    }
    syncs = []
    def sync(**kwargs):
        syncs.append(kwargs)
        number = len(syncs)
        if number not in batches:
            raise MatrixError("stop test", errcode="M_UNAVAILABLE")
        return {"next_batch": str(number), "rooms": {"join": {
            room: {"timeline": {"events": [matrix_event(event_id=event_id)],
                                "limited": room == late and number == 2}}
            for room, event_id in batches[number].items()
        }}}
    reader.sync = sync
    monkeypatch.setattr(ingress, "MatrixClient", lambda *a, **kw: reader)
    monkeypatch.setattr(ingress, "RETRY_MAX", 0)
    caplog.set_level("INFO")
    with pytest.raises(MatrixError):
        await component._matrix()
    assert {note.data["message"]["id"] for note in assistant.read_state().pending} == {"$good", "$late-live"}
    assert any("event=$old-late" in record.message and "reason=initial_sync" in record.message
               for record in caplog.records)
    with pytest.raises(MatrixError):
        await component._matrix()
    assert syncs[4]["since"] == "3"
    assert {note.data["message"]["id"] for note in assistant.read_state().pending} == {
        "$good", "$late-live", "$after-reconnect", "$late-after-reconnect",
    }
    assert reader._session.close.call_count == 2


@pytest.mark.asyncio
async def test_matrix_dm_parent_works_through_ingress_without_backlog(monkeypatch, transports):
    component = manager(monkeypatch, matrix=True)
    reader = transports["matrix"]
    reader.direct_rooms.add(ROOM)
    await component._receive("matrix", ROOM, matrix_event(), reader)
    assert not assistant.read_state().pending
    remember("matrix")
    await component._receive("matrix", ROOM, matrix_event(root="$root", mention=False), reader)
    assert len(assistant.read_state().pending) == 1
    assert not reader.history_calls


@pytest.mark.asyncio
async def test_sdk_ack_failure_drops_without_admission(monkeypatch, transports, caplog):
    component = manager(monkeypatch)
    socket = SimpleNamespace(send_socket_mode_response=AsyncMock(side_effect=RuntimeError("private body")))
    request = SocketModeRequest(type="events_api", envelope_id="one", payload={"event": slack_event()})
    caplog.set_level("INFO")
    await component._slack_event(socket, request)
    assert not assistant.read_state().pending
    assert "reason=RuntimeError" in caplog.text
    assert "private body" not in caplog.text


def test_slack_ingress_timeouts_and_no_rate_limit_sleep():
    import requests
    from slack_chat.client import SlackClient
    client = SlackClient("test", timeout=10, retries=0)
    response = MagicMock()
    response.json.return_value = {"ok": True}
    client.session = MagicMock()
    client.session.request.return_value = response
    assert client._request("GET", "auth.test") == {"ok": True}
    assert client.session.request.call_args.kwargs["timeout"] == 10
    response.status_code = 429
    response.headers = {"Retry-After": "86400"}
    response.raise_for_status.side_effect = requests.HTTPError(response=response)
    with pytest.raises(SlackError) as error:
        client._request("GET", "auth.test")
    assert error.value.code == "ratelimited"
    assert client.session.request.call_count == 2


@pytest.mark.parametrize("log_level", ["info", "trace"])
def test_daemon_run_alone_receives_mentions(monkeypatch, log_level, fake_socket, transports):
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    for key in ("LMER_MATRIX_HOMESERVER", "LMER_MATRIX_USER_ID", "LMER_MATRIX_ACCESS_TOKEN"):
        monkeypatch.delenv(key)
    noop = SimpleNamespace(stop=lambda: None)
    for name in ("_supervise_assistant", "_watch_for_attention"):
        monkeypatch.setattr(daemon, name, lambda *a: noop)
    for name in ("_subscribe_to_comms", "_run_thread_connectors", "_serve_thread_requests"):
        monkeypatch.setattr(daemon, name, lambda *args: noop)
    monkeypatch.setattr(daemon, "reattach_all", lambda **_: [])
    seen = []
    build = api.create_app
    def create(*args, **kwargs):
        seen.append(kwargs["ingress"])
        return build(*args, **kwargs)
    monkeypatch.setattr(daemon, "create_app", create)
    def serve(app, **kwargs):
        with TestClient(app) as client:
            client.portal.call(until, lambda: seen[0].snapshot()["slack"]["up"])
            event = slack_event()
            client.portal.call(fake_socket[0][0].emit, event)
            request = project_requests.listing()[0]
            assert request["state"] == "queued" and request["project_id"] == "general"
            assert len(transports["slack"].posts) == 1
            assert len(assistant.read_state().pending) == 1
            assert assistant.read_state().pending[0].data["workspace_id"] == "general"
            client.portal.call(fake_socket[0][0].emit, event)
            assert len(assistant.read_state().pending) == 1
            secret = cfg.read_secret(cfg.load())
            result = client.get("/api/health", headers={"Authorization": f"Bearer {secret}"})
            assert result.json()["ingress"]["slack"]["state"] == "up"
    monkeypatch.setattr("uvicorn.run", serve)
    assert daemon.main(["run", "--log-level", log_level]) == 0
    assert len(seen) == 1
    assert fake_socket[0][0].closed
