"""Legacy host listeners commit mention intake before acknowledging transport."""
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_sdk.socket_mode.request import SocketModeRequest

from lmer_cli import assistant_mentions
from lmer_platform import ingress, project_requests, projects, store
from matrix_chat import listener as matrix_listener
from slack_chat import listener as slack_listener
from tests.test_assistant_mentions import isolated, transports, slack_event, matrix_event, ROOM  # noqa: F401
from tests.test_daemon_ingress import fake_socket, manager, until  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("disk_failure", [False, True])
async def test_legacy_slack_socket_retains_before_bolt_acknowledgement(monkeypatch, fake_socket, disk_failure):
    projects.migrate()
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-fixture")
    monkeypatch.setattr(slack_listener, "MENTIONS", assistant_mentions.Settings(True))
    monkeypatch.setattr(slack_listener, "build_app", lambda: None)
    monkeypatch.setattr(slack_listener, "_get_bot_user_id", AsyncMock(return_value="UBOT"))
    sessions = SimpleNamespace(max_sessions=1, idle_timeout_minutes=30, run_reaper=AsyncMock(), shutdown_all=AsyncMock())
    monkeypatch.setattr(slack_listener, "session_manager", sessions)
    acknowledgements = []
    class Handler:
        def __init__(self, *_args):
            pass

        async def handle(self, _client, _request):
            path = store.platform_dir() / "ingress.sqlite3"
            rows = []
            if path.exists():
                with sqlite3.connect(path) as db:
                    rows = db.execute("SELECT event FROM inbox WHERE event IS NOT NULL").fetchall()
            acknowledgements.append(len(rows))

        async def start_async(self):
            await self.handle(None, SocketModeRequest(type="events_api", envelope_id="legacy", payload={"event": slack_event()}))

    monkeypatch.setattr("slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler", Handler)
    if disk_failure:
        original = sqlite3.connect
        def unavailable(path, *args, **kwargs):
            if str(path).endswith("ingress.sqlite3"):
                raise sqlite3.OperationalError("Fixture inbox disk failure")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(sqlite3, "connect", unavailable)
        with pytest.raises(sqlite3.OperationalError, match="inbox disk failure"):
            await slack_listener._run()
        assert acknowledgements == []
    else:
        await slack_listener._run()
        assert acknowledgements == [1], "Bolt acknowledged before durable mention intake"
        component = manager(monkeypatch)
        async with component.lifespan(None):
            await until(lambda: len(project_requests.listing()) == 1)
        assert project_requests.listing()[0]["state"] == "queued"
        assert project_requests.listing()[0]["project_id"] == "general"
    sessions.shutdown_all.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_matrix_cursor_and_events_survive_listener_restart(monkeypatch, transports):
    projects.migrate()
    reader = transports["matrix"]
    reader._session = MagicMock()
    reader.whoami = lambda: "@bot:example.org"
    responses = iter([
        {"next_batch": "baseline"},
        {"next_batch": "retained", "rooms": {"join": {ROOM: {"timeline": {"events": [matrix_event()]}}}}},
    ])
    reader.sync = lambda **_kwargs: next(responses)
    settings = matrix_listener.ListenerConfig()
    listener = matrix_listener.Listener(settings, MagicMock(), matrix=reader)
    assert listener.sync_once() == []
    assert listener.sync_once() == [(ROOM, matrix_event())]
    restarted = matrix_listener.Listener(settings, MagicMock(), matrix=reader)
    assert restarted._next_batch == "retained"
    class Disconnected(Exception):
        pass
    def disconnected(**_kwargs):
        raise Disconnected()
    reader.sync = disconnected
    monkeypatch.setattr(ingress, "MatrixClient", lambda *a, **kw: reader)
    component = manager(monkeypatch, matrix=True)
    with pytest.raises(Disconnected):
        await component._matrix()
    assert len(project_requests.listing()) == 1
    assert project_requests.listing()[0]["state"] == "queued"
    assert project_requests.listing()[0]["project_id"] == "general"
    assert len(reader.posts) == 1
