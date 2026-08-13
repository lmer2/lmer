"""Tests for the lmer-session routing in slack_chat.listener.

The Slack app is built lazily (build_app), so importing the module needs no
token; tests replace the module-level ``app`` with a stub and never make real
Slack calls.
"""

import asyncio
import json
import os
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from slack_chat import listener
from slack_chat.sessions import CHAT_TASK_ID, SessionManager, listener_default_preset
from lmer_cli.presets import Preset, load_presets, select_preset_name

PERMALINK = "https://x.slack.com/archives/C1/p1112220000000000"


def _write_presets(path: Path, *names: str) -> Path:
    """Write a presets file defining *names* and return its path."""
    path.write_text(
        json.dumps({name: {"checkout": f"/co/{name}"} for name in names}),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def manager(monkeypatch) -> MagicMock:
    """Replace the module-level session manager with a mock."""
    mgr = MagicMock()
    mgr.touch.return_value = False
    mgr.at_capacity.return_value = False
    mgr.get_active_in_channel.return_value = None
    mgr.spawn = AsyncMock()
    mgr.idle_timeout_minutes = 30
    mgr.max_sessions = 5
    # The ack asks the manager for the listener-wide default, because only the
    # manager knows the .env files the spawned CLI reads (issue #259). Keep the
    # mock on the real environment-only resolver so the ack tests below still
    # exercise the selector vars they set; the file tiers have their own test.
    mgr.default_preset.side_effect = lambda environ=None: listener_default_preset(
        environ
    )
    # Same deal for whether that default is *defined*: the manager answers,
    # because LMER_PRESETS_FILE reaches the child through those same .env tiers
    # (issue #279). The mock keeps to the environment-only half — a listener
    # that exports the presets file its child will read, which is the ordinary
    # deployment — so the ack tests below configure availability the way an
    # operator does; the file tiers have their own test.
    mgr.child_presets.side_effect = lambda environ=None: load_presets()
    monkeypatch.setattr(listener, "session_manager", mgr)
    # The external-session registry check defaults to "no one else is here" so
    # these tests exercise the in-memory paths without touching the real
    # ~/.lmer/slack-sessions registry. Tests that need the connected case
    # override it (see test_external_session_skips_spawn_silently).
    monkeypatch.setattr(listener, "is_thread_connected", lambda *a, **k: False)
    return mgr


@pytest.fixture
def slack_app(monkeypatch) -> types.SimpleNamespace:
    """Replace the module-level Slack app with mocked client methods."""
    client = types.SimpleNamespace(
        chat_getPermalink=AsyncMock(
            return_value={"ok": True, "permalink": PERMALINK}
        ),
        chat_postMessage=AsyncMock(return_value={"ok": True}),
        auth_test=AsyncMock(return_value={"ok": True, "user_id": "U_BOT"}),
    )
    fake_app = types.SimpleNamespace(client=client)
    monkeypatch.setattr(listener, "app", fake_app)
    monkeypatch.setattr(listener, "_bot_user_id", None)
    return fake_app


@pytest.fixture(autouse=True)
def isolate_dm_allowlist(monkeypatch):
    """Isolate the DM allowlist from the ambient environment.

    ``DM_ALLOWED_USERS`` is parsed from ``LMER_SLACK_DM_ALLOWED_USERS`` at import
    time. When the suite runs inside a live Slack session that variable is
    populated (e.g. the operator's own user id), which gates the synthetic test
    users out of the DM-connect paths and fails the tests that assume the CI
    default (empty allowlist = allow all). Reset it to empty before each test;
    tests that exercise gating override it explicitly.
    """
    monkeypatch.setattr(listener, "DM_ALLOWED_USERS", set())


@pytest.fixture(autouse=True)
def isolate_preset_selectors(monkeypatch):
    """Isolate the listener-wide preset default from the ambient environment.

    ``LMER_CHAT_PRESET`` / ``LMER_PRESET`` select a preset for every session
    the listener spawns (issue #181), so the connect ack names them. When the
    suite runs inside a live listener-spawned session either may be set, which
    would add a preset clause to acks these tests assert on. Clear both; the
    tests that exercise the default set it explicitly.
    """
    monkeypatch.delenv("LMER_CHAT_PRESET", raising=False)
    monkeypatch.delenv("LMER_PRESET", raising=False)


@pytest.fixture(autouse=True)
def isolate_presets_file(monkeypatch):
    """Isolate the presets a default is checked against from the ambient env.

    Whether a listener-wide default is defined is now answered from
    ``LMER_PRESETS_FILE`` as the spawned CLI resolves it (issue #279), so a
    real one on the machine running the suite would decide these assertions.
    Tests that need presets point the variable at a file they wrote.
    """
    monkeypatch.delenv("LMER_PRESETS_FILE", raising=False)


class TestCsvEnvSet:
    def test_unset_is_empty(self, monkeypatch):
        monkeypatch.delenv("LMER_SLACK_DM_ALLOWED_USERS", raising=False)
        assert listener._csv_env_set("LMER_SLACK_DM_ALLOWED_USERS") == set()

    def test_parses_trimmed_nonempty(self, monkeypatch):
        monkeypatch.setenv("LMER_SLACK_DM_ALLOWED_USERS", " U1 , ,U2,")
        assert listener._csv_env_set("LMER_SLACK_DM_ALLOWED_USERS") == {"U1", "U2"}


class TestDmUserAllowed:
    def test_open_when_allowlist_empty(self, monkeypatch):
        monkeypatch.setattr(listener, "DM_ALLOWED_USERS", set())
        assert listener._dm_user_allowed("U_ANYONE") is True
        assert listener._dm_user_allowed(None) is True

    def test_gated_when_allowlist_set(self, monkeypatch):
        monkeypatch.setattr(listener, "DM_ALLOWED_USERS", {"U_OK"})
        assert listener._dm_user_allowed("U_OK") is True
        assert listener._dm_user_allowed("U_NOPE") is False
        assert listener._dm_user_allowed(None) is False


class TestConnectLmerSession:
    @pytest.mark.asyncio
    async def test_spawns_session_for_new_thread(self, manager, slack_app):
        say = AsyncMock()

        await listener._connect_lmer_session("C1", "111.222", say)

        slack_app.client.chat_getPermalink.assert_awaited_once_with(
            channel="C1", message_ts="111.222"
        )
        manager.spawn.assert_awaited_once_with("C1", "111.222", PERMALINK, preset=None)
        # An ack is posted in the thread
        say.assert_awaited_once()
        assert say.await_args.kwargs["thread_ts"] == "111.222"
        assert "Connecting" in say.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_active_session_only_touches(self, manager, slack_app):
        manager.touch.return_value = True
        say = AsyncMock()

        await listener._connect_lmer_session("C1", "111.222", say)

        manager.spawn.assert_not_awaited()
        say.assert_not_awaited()
        manager.touch.assert_called_once_with("C1", "111.222")

    @pytest.mark.asyncio
    async def test_external_session_skips_spawn_silently(
        self, manager, slack_app, monkeypatch
    ):
        """An lmer attached to this thread outside the listener (e.g. a manual
        `lmer chat <permalink>`) registers itself; the listener must not spawn a
        second one, and must stay silent — the existing session is handling the
        conversation (issue #74)."""
        monkeypatch.setattr(listener, "is_thread_connected", lambda *a, **k: True)
        say = AsyncMock()

        await listener._connect_lmer_session("C1", "111.222", say)

        manager.spawn.assert_not_awaited()
        say.assert_not_awaited()
        # We bail before even resolving the permalink.
        slack_app.client.chat_getPermalink.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_own_active_session_skips_external_check(
        self, manager, slack_app, monkeypatch
    ):
        """When the listener already tracks a live session for the thread
        (touch() hits), it returns before consulting the registry — its own
        session must never be mistaken for a blocking external one."""
        manager.touch.return_value = True
        external = MagicMock(return_value=True)
        monkeypatch.setattr(listener, "is_thread_connected", external)
        say = AsyncMock()

        await listener._connect_lmer_session("C1", "111.222", say)

        manager.spawn.assert_not_awaited()
        say.assert_not_awaited()
        external.assert_not_called()

    @pytest.mark.asyncio
    async def test_capacity_reached_posts_busy_message(self, manager, slack_app):
        manager.at_capacity.return_value = True
        say = AsyncMock()

        await listener._connect_lmer_session("C1", "111.222", say)

        manager.spawn.assert_not_awaited()
        say.assert_awaited_once()
        assert "can't take on another conversation" in say.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_permalink_failure_posts_error(self, manager, slack_app):
        slack_app.client.chat_getPermalink = AsyncMock(return_value={"ok": False})
        say = AsyncMock()

        await listener._connect_lmer_session("C1", "111.222", say)

        manager.spawn.assert_not_awaited()
        say.assert_awaited_once()
        assert "permalink" in say.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_spawn_failure_posts_error(self, manager, slack_app):
        manager.spawn = AsyncMock(side_effect=RuntimeError("boom"))
        say = AsyncMock()

        await listener._connect_lmer_session("C1", "111.222", say)

        say.assert_awaited_once()
        assert "Could not start a session" in say.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_permalink_fetch_timeout_does_not_spawn(
        self, manager, slack_app, monkeypatch
    ):
        # A hung chat_getPermalink must not wedge the connect path (and, since
        # the fetch runs under _connect_lock, must not serialize every other
        # new-thread connect). A timed-out fetch is treated as a fetch failure.
        async def _hang(**kwargs):
            await asyncio.sleep(10)

        slack_app.client.chat_getPermalink = AsyncMock(side_effect=_hang)
        monkeypatch.setattr(listener, "PERMALINK_FETCH_TIMEOUT_SECONDS", 0.01)
        say = AsyncMock()

        await listener._connect_lmer_session("C1", "111.222", say)

        manager.spawn.assert_not_awaited()
        say.assert_awaited_once()
        assert "permalink" in say.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_dm_ack_tells_user_to_reply_in_thread(self, manager, slack_app):
        say = AsyncMock()

        await listener._connect_lmer_session("D1", "111.222", say, is_dm=True)

        manager.spawn.assert_awaited_once()
        assert "in this thread" in say.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_dedup_channel_points_at_active_session(self, manager, slack_app):
        """dedup_channel: a top-level DM with a live channel session is pointed
        at that session's thread instead of spawning a second container."""
        manager.get_active_in_channel.return_value = types.SimpleNamespace(
            channel="D1", thread_ts="000.111", permalink=PERMALINK
        )
        say = AsyncMock()

        await listener._connect_lmer_session(
            "D1", "111.222", say, is_dm=True, dedup_channel=True
        )

        manager.spawn.assert_not_awaited()
        say.assert_awaited_once()
        assert PERMALINK in say.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_dedup_channel_same_thread_falls_through_to_touch(
        self, manager, slack_app
    ):
        """A top-level DM whose live session is in THIS thread (the @mention
        double-event race) must not post a redundant 'continue in this thread'
        pointer - it falls through to the touch() path instead."""
        manager.get_active_in_channel.return_value = types.SimpleNamespace(
            channel="D1", thread_ts="111.222", permalink=PERMALINK
        )
        manager.touch.return_value = True
        say = AsyncMock()

        await listener._connect_lmer_session(
            "D1", "111.222", say, is_dm=True, dedup_channel=True
        )

        manager.spawn.assert_not_awaited()
        say.assert_not_awaited()
        manager.touch.assert_called_once_with("D1", "111.222")

    @pytest.mark.asyncio
    async def test_dedup_channel_spawns_when_no_active_session(self, manager, slack_app):
        """dedup_channel with no live session in the channel spawns normally."""
        manager.get_active_in_channel.return_value = None
        say = AsyncMock()

        await listener._connect_lmer_session(
            "D1", "111.222", say, is_dm=True, dedup_channel=True
        )

        manager.spawn.assert_awaited_once_with("D1", "111.222", PERMALINK, preset=None)

    @pytest.mark.asyncio
    async def test_concurrent_top_level_dms_spawn_once(self, manager, slack_app):
        """Two simultaneous top-level DMs (distinct ts) spawn one session.

        Each top-level message keys on its own ts so per-thread touch() can't
        dedup them; the channel-wide dedup inside the connect lock must. The
        first spawn registers a channel session; the second, serialized by the
        lock, then sees get_active_in_channel and is pointed at the thread.
        """
        say = AsyncMock()
        active = types.SimpleNamespace(
            channel="D1", thread_ts="111.222", permalink=PERMALINK
        )

        async def spawn_and_register(channel, thread_ts, permalink, preset=None):
            manager.get_active_in_channel.return_value = active

        manager.spawn = AsyncMock(side_effect=spawn_and_register)

        await asyncio.gather(
            listener._connect_lmer_session(
                "D1", "111.222", say, is_dm=True, dedup_channel=True
            ),
            listener._connect_lmer_session(
                "D1", "333.444", say, is_dm=True, dedup_channel=True
            ),
        )

        manager.spawn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_concurrent_connects_spawn_once(self, manager, slack_app):
        """Two simultaneous connect attempts for one thread spawn one session.

        The first spawn registers the session; once it has, touch() returns
        True, so the second attempt (serialized by the connect lock) must
        treat the thread as already served instead of erroring.
        """
        say = AsyncMock()

        async def spawn_and_register(channel, thread_ts, permalink, preset=None):
            manager.touch.return_value = True

        manager.spawn = AsyncMock(side_effect=spawn_and_register)

        await asyncio.gather(
            listener._connect_lmer_session("C1", "111.222", say),
            listener._connect_lmer_session("C1", "111.222", say),
        )

        manager.spawn.assert_awaited_once()
        # Only the winning attempt posts the connect ack - no error message.
        say.assert_awaited_once()
        assert "Connecting" in say.await_args.kwargs["text"]


class TestHandleMention:
    @pytest.mark.asyncio
    async def test_mention_outside_thread_uses_message_ts(
        self, manager, slack_app, monkeypatch
    ):
        connect = AsyncMock()
        monkeypatch.setattr(listener, "_connect_lmer_session", connect)
        say = AsyncMock()

        event = {"user": "U1", "channel": "C1", "ts": "111.222"}
        await listener.handle_mention(event, say)

        connect.assert_awaited_once_with(
            "C1", "111.222", say, is_dm=False, preset_name=None
        )

    @pytest.mark.asyncio
    async def test_mention_inside_thread_uses_thread_ts(
        self, manager, slack_app, monkeypatch
    ):
        connect = AsyncMock()
        monkeypatch.setattr(listener, "_connect_lmer_session", connect)
        say = AsyncMock()

        event = {
            "user": "U1",
            "channel": "C1",
            "ts": "333.444",
            "thread_ts": "111.222",
        }
        await listener.handle_mention(event, say)

        connect.assert_awaited_once_with(
            "C1", "111.222", say, is_dm=False, preset_name=None
        )

    @pytest.mark.asyncio
    async def test_bot_authored_mention_is_ignored(
        self, manager, slack_app, monkeypatch
    ):
        """A bot-authored message quoting @bot must not spawn a session."""
        connect = AsyncMock()
        monkeypatch.setattr(listener, "_connect_lmer_session", connect)

        event = {"bot_id": "B999", "channel": "C1", "ts": "111.222"}
        await listener.handle_mention(event, AsyncMock())

        connect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dm_mention_from_non_allowlisted_user_is_ignored(
        self, manager, slack_app, monkeypatch
    ):
        """A DM mention from a user off the allowlist must not spawn a session."""
        monkeypatch.setattr(listener, "DM_ALLOWED_USERS", {"U_OK"})
        connect = AsyncMock()
        monkeypatch.setattr(listener, "_connect_lmer_session", connect)

        event = {"user": "U_NOPE", "channel": "D1", "ts": "111.222"}
        await listener.handle_mention(event, AsyncMock())

        connect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_channel_mention_not_gated_by_dm_allowlist(
        self, manager, slack_app, monkeypatch
    ):
        """The DM allowlist must never gate a mention in a real channel."""
        monkeypatch.setattr(listener, "DM_ALLOWED_USERS", {"U_OK"})
        connect = AsyncMock()
        monkeypatch.setattr(listener, "_connect_lmer_session", connect)
        say = AsyncMock()

        event = {"user": "U_NOPE", "channel": "C1", "ts": "111.222"}
        await listener.handle_mention(event, say)

        connect.assert_awaited_once_with(
            "C1", "111.222", say, is_dm=False, preset_name=None
        )

    @pytest.mark.asyncio
    async def test_dm_mention_connects_with_is_dm(
        self, manager, slack_app, monkeypatch
    ):
        """A mention inside a DM connects with is_dm=True so the reply-in-thread
        hint is shown, mirroring the plain-message DM path."""
        connect = AsyncMock()
        monkeypatch.setattr(listener, "_connect_lmer_session", connect)
        say = AsyncMock()

        event = {"user": "U1", "channel": "D1", "ts": "111.222"}
        await listener.handle_mention(event, say)

        connect.assert_awaited_once_with(
            "D1", "111.222", say, is_dm=True, preset_name=None
        )


class TestHandleMessageEvent:
    @pytest.mark.asyncio
    async def test_any_thread_message_touches_session(self, manager, slack_app):
        """Channel messages (including the agent's own bot posts) reset the idle timer."""
        say = AsyncMock()
        event = {
            "channel": "C1",
            "channel_type": "channel",
            "ts": "333.444",
            "thread_ts": "111.222",
            "bot_id": "B999",
        }
        await listener.handle_message_event(event, say)

        manager.touch.assert_called_once_with("C1", "111.222")
        say.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dm_message_connects_session(self, manager, slack_app, monkeypatch):
        connect = AsyncMock()
        monkeypatch.setattr(listener, "_connect_lmer_session", connect)
        say = AsyncMock()

        event = {
            "channel": "D1",
            "channel_type": "im",
            "user": "U1",
            "ts": "111.222",
            "text": "hello there",
        }
        await listener.handle_message_event(event, say)

        connect.assert_awaited_once_with(
            "D1", "111.222", say, is_dm=True, dedup_channel=True, preset_name=None
        )

    @pytest.mark.asyncio
    async def test_threaded_dm_reply_skips_channel_dedup(
        self, manager, slack_app, monkeypatch
    ):
        """A DM reply inside a thread keys on its real thread_ts and so does
        not request channel-wide dedup (dedup_channel=False)."""
        connect = AsyncMock()
        monkeypatch.setattr(listener, "_connect_lmer_session", connect)

        say = AsyncMock()
        event = {
            "channel": "D1",
            "channel_type": "im",
            "user": "U1",
            "ts": "333.444",
            "thread_ts": "111.222",
            "text": "continuing here",
        }
        await listener.handle_message_event(event, say)

        connect.assert_awaited_once_with(
            "D1", "111.222", say, is_dm=True, dedup_channel=False, preset_name=None
        )

    @pytest.mark.asyncio
    async def test_dm_command_like_message_still_connects(
        self, manager, slack_app, monkeypatch
    ):
        """The generic listener carries no commands: a '!'-prefixed DM connects
        a session like any other (unlike the standup bot, which reserved '!')."""
        connect = AsyncMock()
        monkeypatch.setattr(listener, "_connect_lmer_session", connect)

        event = {
            "channel": "D1",
            "channel_type": "im",
            "user": "U1",
            "ts": "111.222",
            "text": "!standup week",
        }
        await listener.handle_message_event(event, AsyncMock())

        connect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dm_from_non_allowlisted_user_is_ignored(
        self, manager, slack_app, monkeypatch
    ):
        """When an allowlist is set, an off-list DM connects nothing and stays silent."""
        monkeypatch.setattr(listener, "DM_ALLOWED_USERS", {"U_OK"})
        connect = AsyncMock()
        monkeypatch.setattr(listener, "_connect_lmer_session", connect)
        say = AsyncMock()

        event = {
            "channel": "D1",
            "channel_type": "im",
            "user": "U_NOPE",
            "ts": "111.222",
            "text": "let me in",
        }
        await listener.handle_message_event(event, say)

        connect.assert_not_awaited()
        say.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dm_from_allowlisted_user_connects(
        self, manager, slack_app, monkeypatch
    ):
        """A DM from a user on the allowlist connects a session as usual."""
        monkeypatch.setattr(listener, "DM_ALLOWED_USERS", {"U_OK"})
        connect = AsyncMock()
        monkeypatch.setattr(listener, "_connect_lmer_session", connect)
        say = AsyncMock()

        event = {
            "channel": "D1",
            "channel_type": "im",
            "user": "U_OK",
            "ts": "111.222",
            "text": "hello there",
        }
        await listener.handle_message_event(event, say)

        connect.assert_awaited_once_with(
            "D1", "111.222", say, is_dm=True, dedup_channel=True, preset_name=None
        )

    @pytest.mark.asyncio
    async def test_dm_bot_message_is_skipped(self, manager, slack_app, monkeypatch):
        connect = AsyncMock()
        monkeypatch.setattr(listener, "_connect_lmer_session", connect)

        event = {
            "channel": "D1",
            "channel_type": "im",
            "bot_id": "B999",
            "ts": "111.222",
            "text": "hello",
        }
        await listener.handle_message_event(event, AsyncMock())

        connect.assert_not_awaited()
        # But it still counts as thread activity
        manager.touch.assert_called_once_with("D1", "111.222")

    @pytest.mark.asyncio
    async def test_dm_message_subtype_is_skipped(self, manager, slack_app, monkeypatch):
        """An edited/changed DM (carries a subtype) does not spawn a session."""
        connect = AsyncMock()
        monkeypatch.setattr(listener, "_connect_lmer_session", connect)

        event = {
            "channel": "D1",
            "channel_type": "im",
            "subtype": "message_changed",
            "user": "U1",
            "ts": "111.222",
            "text": "edited",
        }
        await listener.handle_message_event(event, AsyncMock())

        connect.assert_not_awaited()


class TestDisconnectNotices:
    @pytest.mark.asyncio
    async def test_idle_notice_posts_reconnect_hint_with_bot_mention(
        self, manager, slack_app
    ):
        session = types.SimpleNamespace(channel="C1", thread_ts="111.222")

        await listener._post_idle_disconnect_notice(session)

        slack_app.client.chat_postMessage.assert_awaited_once()
        kwargs = slack_app.client.chat_postMessage.await_args.kwargs
        assert kwargs["channel"] == "C1"
        assert kwargs["thread_ts"] == "111.222"
        assert "<@U_BOT>" in kwargs["text"]
        assert "30 minutes" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_crash_notice_posts_reconnect_hint(self, manager, slack_app):
        session = types.SimpleNamespace(channel="C1", thread_ts="111.222")

        await listener._post_crash_disconnect_notice(session)

        slack_app.client.chat_postMessage.assert_awaited_once()
        kwargs = slack_app.client.chat_postMessage.await_args.kwargs
        assert kwargs["thread_ts"] == "111.222"
        assert "ended unexpectedly" in kwargs["text"]
        assert "<@U_BOT>" in kwargs["text"]


class TestBuildAppAndMain:
    def test_build_app_registers_handlers(self, monkeypatch):
        registered = {}

        class FakeApp:
            def __init__(self, token=None):
                self.token = token

            def event(self, name):
                def deco(fn):
                    registered[name] = fn
                    return fn

                return deco

        import slack_bolt.async_app as async_app_mod

        monkeypatch.setattr(async_app_mod, "AsyncApp", FakeApp)

        built = listener.build_app(token="xoxb-test")
        assert isinstance(built, FakeApp)
        assert built.token == "xoxb-test"
        assert registered["app_mention"] is listener.handle_mention
        assert registered["message"] is listener.handle_message_event

    def test_main_without_bot_token_returns_1(self, monkeypatch):
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        # Don't let .env on the host re-supply the token during the test.
        monkeypatch.setattr(listener, "_load_env_files", lambda: None)
        assert listener.main([]) == 1

    def test_main_without_app_token_returns_1(self, monkeypatch):
        # Bot token present, app token missing: main() must fail fast up front
        # (rather than letting a ValueError bubble out of asyncio.run(_run))
        # so the broad except over asyncio.run can stay narrowed to
        # KeyboardInterrupt.
        monkeypatch.setattr(listener, "_load_env_files", lambda: None)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
        # If validation fails to short-circuit, asyncio.run would be reached;
        # make that loud rather than a real socket-mode connection attempt.
        monkeypatch.setattr(
            listener.asyncio,
            "run",
            lambda *a, **k: pytest.fail("asyncio.run should not be reached"),
        )
        assert listener.main([]) == 1


class TestLoadEnvFiles:
    """Env collection mirrors the main lmer CLI: active env > cwd .env > ~/.lmer/.env."""

    def _stub_state_dir(self, monkeypatch, state_dir):
        import lmer_cli.runtime as rt

        monkeypatch.setattr(rt, "lmer_state_dir", lambda: state_dir)

    def test_active_env_wins_over_files(self, tmp_path, monkeypatch):
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        (cwd / ".env").write_text("LMER_SLACK_ENVTEST=from_cwd\n")
        state = tmp_path / "state"
        state.mkdir()
        (state / ".env").write_text("LMER_SLACK_ENVTEST=from_state\n")
        monkeypatch.chdir(cwd)
        self._stub_state_dir(monkeypatch, state)
        monkeypatch.setenv("LMER_SLACK_ENVTEST", "from_active")

        listener._load_env_files()

        assert os.environ["LMER_SLACK_ENVTEST"] == "from_active"

    def test_cwd_beats_state_dir_and_state_only_keys_load(self, tmp_path, monkeypatch):
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        (cwd / ".env").write_text("LMER_SLACK_ENVTEST=from_cwd\n")
        state = tmp_path / "state"
        state.mkdir()
        (state / ".env").write_text(
            "LMER_SLACK_ENVTEST=from_state\nLMER_SLACK_STATEONLY=yes\n"
        )
        monkeypatch.chdir(cwd)
        self._stub_state_dir(monkeypatch, state)
        monkeypatch.delenv("LMER_SLACK_ENVTEST", raising=False)
        monkeypatch.delenv("LMER_SLACK_STATEONLY", raising=False)

        listener._load_env_files()

        assert os.environ["LMER_SLACK_ENVTEST"] == "from_cwd"
        assert os.environ["LMER_SLACK_STATEONLY"] == "yes"

    def test_no_env_files_is_noop(self, tmp_path, monkeypatch):
        """No .env anywhere: nothing loaded, no error (active env still applies)."""
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        state = tmp_path / "state"
        state.mkdir()
        monkeypatch.chdir(cwd)
        self._stub_state_dir(monkeypatch, state)

        listener._load_env_files()  # must not raise


class TestEnsureCaBundle:
    def test_sets_ssl_cert_file_to_certifi_when_unset(self, monkeypatch):
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)

        listener._ensure_ca_bundle()

        import certifi

        assert os.environ["SSL_CERT_FILE"] == certifi.where()

    def test_leaves_existing_ssl_cert_file_untouched(self, monkeypatch):
        monkeypatch.setenv("SSL_CERT_FILE", "/custom/corporate-ca.pem")

        listener._ensure_ca_bundle()

        assert os.environ["SSL_CERT_FILE"] == "/custom/corporate-ca.pem"


class TestPresetSelection:
    """Resolving a $preset:<name> token: known presets reach spawn(), unknown
    names are rejected, and the token is parsed off the triggering message."""

    @pytest.mark.asyncio
    async def test_known_preset_passed_to_spawn(self, manager, slack_app, monkeypatch):
        preset = Preset(name="my_service", checkout="/co", service="svc")
        monkeypatch.setattr(listener, "PRESETS", {"my_service": preset})
        say = AsyncMock()

        await listener._connect_lmer_session(
            "C1", "111.222", say, preset_name="my_service"
        )

        manager.spawn.assert_awaited_once_with(
            "C1", "111.222", PERMALINK, preset=preset
        )
        # The ack names the applied preset.
        assert "my_service" in say.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_unknown_preset_rejected_without_spawn(
        self, manager, slack_app, monkeypatch
    ):
        monkeypatch.setattr(
            listener,
            "PRESETS",
            {"my_service": Preset(name="my_service", checkout="/co")},
        )
        say = AsyncMock()

        await listener._connect_lmer_session("C1", "111.222", say, preset_name="bogus")

        manager.spawn.assert_not_awaited()
        say.assert_awaited_once()
        text = say.await_args.kwargs["text"]
        assert "Unknown preset" in text
        assert "bogus" in text
        assert "my_service" in text  # lists what IS available

    @pytest.mark.asyncio
    async def test_unknown_preset_with_none_configured(
        self, manager, slack_app, monkeypatch
    ):
        monkeypatch.setattr(listener, "PRESETS", {})
        say = AsyncMock()

        await listener._connect_lmer_session("C1", "111.222", say, preset_name="x")

        manager.spawn.assert_not_awaited()
        assert "none configured" in say.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_preset_on_active_thread_is_ignored(
        self, manager, slack_app, monkeypatch
    ):
        """A $preset token on a thread that already has a live session is moot:
        touch() wins, so nothing is spawned and nothing is rejected."""
        manager.touch.return_value = True
        monkeypatch.setattr(listener, "PRESETS", {})
        say = AsyncMock()

        await listener._connect_lmer_session(
            "C1", "111.222", say, preset_name="anything"
        )

        manager.spawn.assert_not_awaited()
        say.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mention_token_is_parsed_and_passed(
        self, manager, slack_app, monkeypatch
    ):
        connect = AsyncMock()
        monkeypatch.setattr(listener, "_connect_lmer_session", connect)
        say = AsyncMock()

        event = {
            "user": "U1",
            "channel": "C1",
            "ts": "111.222",
            "text": "<@U_BOT> $preset:my_service please do X",
        }
        await listener.handle_mention(event, say)

        connect.assert_awaited_once_with(
            "C1", "111.222", say, is_dm=False, preset_name="my_service"
        )

    @pytest.mark.asyncio
    async def test_dm_token_is_parsed_and_passed(
        self, manager, slack_app, monkeypatch
    ):
        connect = AsyncMock()
        monkeypatch.setattr(listener, "_connect_lmer_session", connect)
        say = AsyncMock()

        event = {
            "channel": "D1",
            "channel_type": "im",
            "user": "U1",
            "ts": "111.222",
            "text": "$preset:my_service hello",
        }
        await listener.handle_message_event(event, say)

        connect.assert_awaited_once_with(
            "D1",
            "111.222",
            say,
            is_dm=True,
            dedup_channel=True,
            preset_name="my_service",
        )


class TestListenerWideDefaultInAck:
    """The connect ack names whichever preset the session actually gets
    (issue #181).

    ``LMER_CHAT_PRESET`` in the listener's environment applies to every session
    it spawns. Both the honoured case and the displaced case are invisible from
    Slack unless the ack says so, and the person who needs to know is the one
    who just typed the message — not whoever later reads
    ``lmer_session_spawned``.
    """

    @pytest.mark.asyncio
    async def test_default_alone_is_named(
        self, manager, slack_app, monkeypatch, tmp_path
    ):
        """The silently-honoured case: no token, but a preset still applies."""
        monkeypatch.setenv(
            "LMER_PRESETS_FILE", str(_write_presets(tmp_path / "p.json", "wide"))
        )
        monkeypatch.setenv("LMER_CHAT_PRESET", "wide")
        say = AsyncMock()

        await listener._connect_lmer_session("C1", "111.222", say)

        text = say.await_args.kwargs["text"]
        assert "listener default preset `wide`" in text
        assert "`LMER_CHAT_PRESET`" in text

    @pytest.mark.asyncio
    async def test_token_says_it_replaced_the_default(
        self, manager, slack_app, monkeypatch
    ):
        monkeypatch.setattr(
            listener,
            "PRESETS",
            {
                "wide": Preset(name="wide", checkout="/wide"),
                "chosen": Preset(name="chosen", checkout="/chosen"),
            },
        )
        monkeypatch.setenv("LMER_CHAT_PRESET", "wide")
        say = AsyncMock()

        await listener._connect_lmer_session(
            "C1", "111.222", say, preset_name="chosen"
        )

        text = say.await_args.kwargs["text"]
        assert "using preset `chosen`" in text
        assert "replacing the listener default `wide`" in text, (
            "a token that displaced a default must say so — otherwise the "
            "operator's default silently vanishes for that session"
        )

    @pytest.mark.asyncio
    async def test_token_matching_the_default_is_not_called_a_replacement(
        self, manager, slack_app, monkeypatch
    ):
        monkeypatch.setattr(
            listener, "PRESETS", {"wide": Preset(name="wide", checkout="/wide")}
        )
        monkeypatch.setenv("LMER_CHAT_PRESET", "wide")
        say = AsyncMock()

        await listener._connect_lmer_session("C1", "111.222", say, preset_name="wide")

        text = say.await_args.kwargs["text"]
        assert "using preset `wide`" in text
        assert "replacing" not in text

    @pytest.mark.asyncio
    async def test_no_preset_anywhere_keeps_the_plain_ack(
        self, manager, slack_app, monkeypatch
    ):
        monkeypatch.setattr(listener, "PRESETS", {})
        say = AsyncMock()

        await listener._connect_lmer_session("C1", "111.222", say)

        text = say.await_args.kwargs["text"]
        assert "preset" not in text

    @pytest.mark.asyncio
    async def test_generic_var_is_named_as_its_own_source(
        self, manager, slack_app, monkeypatch, tmp_path
    ):
        monkeypatch.setenv(
            "LMER_PRESETS_FILE", str(_write_presets(tmp_path / "p.json", "wide"))
        )
        monkeypatch.setenv("LMER_PRESET", "wide")
        say = AsyncMock()

        await listener._connect_lmer_session("C1", "111.222", say)

        assert "`LMER_PRESET`" in say.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_undefined_default_warns_but_still_spawns(
        self, manager, slack_app, monkeypatch, tmp_path
    ):
        """An operator misconfiguration, not a user error: the child CLI exits
        2 on an unknown preset, so the session dies moments after the ack. Say
        why in the thread rather than only in the listener's log."""
        monkeypatch.setenv(
            "LMER_PRESETS_FILE", str(_write_presets(tmp_path / "p.json", "real"))
        )
        monkeypatch.setenv("LMER_CHAT_PRESET", "typo")
        say = AsyncMock()

        await listener._connect_lmer_session("C1", "111.222", say)

        manager.spawn.assert_awaited_once()
        text = say.await_args.kwargs["text"]
        assert "`typo`" in text
        assert "is not defined" in text
        assert "real" in text, "the warning lists what is actually available"


def _what_the_child_would_do(
    manager: SessionManager,
) -> tuple[tuple[str | None, str | None], dict[str, Preset]]:
    """Run the spawned CLI's own startup over *manager*'s tiers.

    ``apply_env_file_defaults`` then ``select_preset_name`` and
    ``load_presets()`` is verbatim what ``lmer`` main() does, so this is the
    session the ack is describing: which preset it selects, and which presets
    it can find. Runs against a copy of the environment the child inherits, so
    the seeding — which writes to ``os.environ`` — cannot touch the suite's.
    """
    from lmer_cli.cli import apply_env_file_defaults

    child_env = dict(os.environ)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "environ", child_env)
        apply_env_file_defaults(manager._child_env_file_candidates())
        return (
            select_preset_name(None, CHAT_TASK_ID, child_env),
            load_presets(),
        )


class TestForwardedEnvFileDefaultInAck:
    """A default that lives only in the forwarded ``--env-file`` is named in
    the ack too (issue #259) — and judged against the presets file the *child*
    resolves, not the listener's (issue #279).

    The listener never loads that file into its own environment — it hands the
    path to the child as ``--env-file`` — so a default living only there
    applied to the session while the ack said nothing about it. The same gap
    then made the ack's verdict on that default wrong in the fatal direction:
    the name was resolved through the child's tiers while "is it defined?" was
    answered from the listener's own presets.
    """

    @pytest.fixture(autouse=True)
    def isolated_state_dir(self, monkeypatch, tmp_path: Path) -> Path:
        """``~/.lmer/.env`` is one of the tiers the resolution models; a real
        one on this machine must not configure a preset for these tests."""
        from lmer_cli import runtime

        state = tmp_path / "state"
        state.mkdir()
        monkeypatch.setattr(runtime, "_LMER_STATE_DIR", state)
        return state

    @pytest.fixture
    def forward(self, monkeypatch, tmp_path: Path):
        """Install a listener whose sessions get *body* as ``--env-file``."""

        def _forward(body: str) -> SessionManager:
            env_file = tmp_path / "deploy.env"
            env_file.write_text(body, encoding="utf-8")
            manager = SessionManager(
                spawn_cwd=str(tmp_path / "cwd"),
                log_dir=str(tmp_path / "logs"),
                lmer_env_file=str(env_file),
            )
            monkeypatch.setattr(listener, "session_manager", manager)
            return manager

        return _forward

    def test_ack_names_a_default_only_the_child_would_load(
        self, monkeypatch, tmp_path, forward
    ):
        """#259's case: the selector is in the forwarded file, the presets file
        is exported the ordinary way."""
        monkeypatch.setenv(
            "LMER_PRESETS_FILE", str(_write_presets(tmp_path / "p.json", "house"))
        )
        forward("LMER_CHAT_PRESET=house\n")

        clause, warning = listener._preset_ack_parts(None)

        assert "listener default preset `house`" in clause
        assert "`LMER_CHAT_PRESET`" in clause
        assert warning == "", "a default that is defined is not a warning"

    def test_a_forwarded_presets_file_is_not_called_undefined(
        self, monkeypatch, tmp_path, forward
    ):
        """#279's repro: selector *and* presets file live only in the forwarded
        ``--env-file``.

        The listener's own environment sees neither, so its module-level
        ``PRESETS`` is empty — and answering "is `house` defined?" from there
        told the person who just typed the message that their session would
        fail to start, seconds before the child loaded that very preset and
        started fine.
        """
        presets = _write_presets(tmp_path / "p.json", "house")
        manager = forward(f"LMER_CHAT_PRESET=house\nLMER_PRESETS_FILE={presets}\n")
        monkeypatch.setattr(listener, "PRESETS", {})

        clause, warning = listener._preset_ack_parts(None)

        assert "listener default preset `house`" in clause
        assert warning == "", (
            "the child loads this preset from the forwarded file, so a fatal "
            "verdict here is confidently wrong"
        )
        assert "fail to start" not in clause + warning

        # And that is not just a nicer-sounding claim: the child really does
        # select `house` and really does find it defined.
        selected, child_presets = _what_the_child_would_do(manager)
        assert selected == ("house", "LMER_CHAT_PRESET")
        assert "house" in child_presets

    def test_a_default_the_child_cannot_resolve_still_warns(
        self, monkeypatch, tmp_path, forward
    ):
        """The warning is not weakened: with no presets file the child finds
        nothing either, so "will fail to start" is the truth."""
        manager = forward("LMER_CHAT_PRESET=house\n")
        monkeypatch.setattr(
            listener, "PRESETS", {"house": Preset(name="house", checkout="/co")}
        )

        clause, warning = listener._preset_ack_parts(None)

        assert clause == ""
        assert "`house`" in warning and "is not defined" in warning
        assert "(none configured)" in warning

        selected, child_presets = _what_the_child_would_do(manager)
        assert selected == ("house", "LMER_CHAT_PRESET")
        assert child_presets == {}, (
            "the listener holding a `house` preset is beside the point — the "
            "child is the one that has to find it"
        )

    def test_a_missing_forwarded_presets_file_still_warns(
        self, monkeypatch, tmp_path, forward
    ):
        """Same for a presets file that is configured but unreadable: the
        child's loader is forgiving, so the session starts with nothing
        defined and exits 2 on the unknown name."""
        forward(
            "LMER_CHAT_PRESET=house\n"
            f"LMER_PRESETS_FILE={tmp_path / 'gone.json'}\n"
        )

        clause, warning = listener._preset_ack_parts(None)

        assert clause == ""
        assert "is not defined" in warning

    def test_the_warning_lists_the_childs_presets(
        self, monkeypatch, tmp_path, forward
    ):
        """When it does warn, it lists what the *session* would have had — a
        list from the listener's own presets would send the operator looking
        for a name their sessions cannot select."""
        presets = _write_presets(tmp_path / "p.json", "child_only")
        forward(f"LMER_CHAT_PRESET=typo\nLMER_PRESETS_FILE={presets}\n")
        monkeypatch.setattr(
            listener,
            "PRESETS",
            {"listener_only": Preset(name="listener_only", checkout="/co")},
        )

        _clause, warning = listener._preset_ack_parts(None)

        assert "child_only" in warning
        assert "listener_only" not in warning


class TestMainLmerEnvFile:
    """main() wires --lmer-env-file through to the SessionManager (issue #75)."""

    def _patch_main(self, monkeypatch, captured):
        class FakeManager:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        monkeypatch.setattr(listener, "SessionManager", FakeManager)
        monkeypatch.setattr(listener, "_load_env_files", lambda: None)
        monkeypatch.setattr(listener, "_ensure_ca_bundle", lambda: None)
        # Don't actually run the event loop; close the coroutine so calling
        # the real _run() (to build the coroutine) raises no "never awaited".
        monkeypatch.setattr(listener.asyncio, "run", lambda coro: coro.close())
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")

    def test_flag_passed_to_session_manager(self, monkeypatch):
        captured: dict = {}
        self._patch_main(monkeypatch, captured)
        rc = listener.main(["--lmer-env-file", "/x/y.env"])
        assert rc == 0
        assert captured.get("lmer_env_file") == "/x/y.env"

    def test_defaults_to_none_when_flag_absent(self, monkeypatch):
        captured: dict = {}
        self._patch_main(monkeypatch, captured)
        rc = listener.main([])
        assert rc == 0
        assert captured.get("lmer_env_file") is None
