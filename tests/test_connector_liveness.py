"""Quiet bus activity reaches the listener reaper without a thread post."""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lmer_cli import comms, connector_ownership
from slack_chat import sessions
from tests.connector_fakes import MatrixRig, SlackRig, isolated_connectors


@pytest.fixture(params=["slack", "matrix"])
def quiet_rig(request, tmp_path):
    rig = (SlackRig if request.param == "slack" else MatrixRig)(tmp_path)
    rig.connector._bus_poll_seconds = 0
    rig.connector._ensure_ready()
    yield request.param, rig
    assert rig.connector.stop(timeout=30, announce=False)
    rig.bus._session._http.close()
    rig.log.close()


def test_notes_stay_on_the_bus_without_thread_posts(quiet_rig):
    channel, rig = quiet_rig
    note = rig.agent("note", "still working")
    rig.connector._outbound_once()

    assert rig.log.read_since(0)[0][0].id == note.id
    assert rig.log.subscribers()[rig.connector.subscriber] == note.seq
    assert (rig.slack.posted if channel == "slack" else rig.matrix.sent) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("session_kind", ["worker", "assistant"])
async def test_quiet_note_keeps_owner_alive_then_real_inactivity_expires(
    quiet_rig, monkeypatch, tmp_path, session_kind,
):
    channel, rig = quiet_rig
    clock = SimpleNamespace(monotonic=lambda: now[0])
    now = [10000.0]
    monkeypatch.setattr(connector_ownership, "time", clock)
    monkeypatch.setattr(sessions, "time", clock)
    manager = sessions.SessionManager(idle_timeout_minutes=30, transport=channel)
    key = rig.connector._lease.key
    session = sessions.Session(
        channel=key.conversation, thread_ts=key.root, permalink="unused",
        process=SimpleNamespace(pid=os.getpid(), returncode=None), master_fd=-1,
        log_path=tmp_path / "unused.log", last_activity=now[0] - 31 * 60,
    )
    manager._sessions[session.key] = session
    terminate = AsyncMock()
    monkeypatch.setattr(manager, "terminate", terminate)

    rig.log.agent_channel = "uber-lmer" if session_kind == "assistant" else "agent"
    rig.log.append(kind="note", text="still working", source=comms.Source("agent"), session_caller=True)
    rig.connector._outbound_once()
    await manager.reap()
    terminate.assert_not_awaited()
    assert session.last_activity == now[0]
    assert (rig.slack.posted if channel == "slack" else rig.matrix.sent) == []

    now[0] += 31 * 60
    rig.connector._outbound_once()
    await manager.reap()
    terminate.assert_awaited_once_with(session)


@pytest.mark.asyncio
async def test_another_process_activity_cannot_keep_a_listener_session_alive(
    quiet_rig, monkeypatch, tmp_path,
):
    channel, rig = quiet_rig
    manager = sessions.SessionManager(idle_timeout_minutes=30, transport=channel)
    key = rig.connector._lease.key
    session = sessions.Session(
        channel=key.conversation, thread_ts=key.root, permalink="unused",
        process=SimpleNamespace(pid=os.getpid() + 100000, returncode=None), master_fd=-1,
        log_path=tmp_path / "unused.log", last_activity=0,
    )
    manager._sessions[session.key] = session
    terminate = AsyncMock()
    monkeypatch.setattr(manager, "terminate", terminate)
    rig.agent("note", "other session")
    rig.connector._outbound_once()

    await manager.reap()

    terminate.assert_awaited_once_with(session)


def test_replayed_notes_and_empty_polls_are_not_new_activity(quiet_rig):
    channel, rig = quiet_rig
    rig.agent("note", "before restart")
    old = rig.connector
    assert old.stop(timeout=30, announce=False)
    remote = {"slack": rig.slack} if channel == "slack" else {"matrix": rig.matrix}
    current = type(old)(old.config, bus_client=rig.bus, **remote)
    rig.connector = current
    current._bus_poll_seconds = 0
    current._ensure_ready()

    current._outbound_once()
    current._outbound_once()

    assert "activity_at" not in current._lease.validate()
    assert (rig.slack.posted if channel == "slack" else rig.matrix.sent) == []
