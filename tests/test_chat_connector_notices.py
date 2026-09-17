"""What a chat thread is told about being listened to (issue #412, T4).

One announcement per attachment, however the attachment was made, and one line
when it ends. A listener that spawned the session announces that thread itself —
at once, and naming the preset the session got — so it tells its child's
connector to stay quiet; every other attachment (a live connect from the UI, a
launch-time target, a shell ``lmer chat``) is announced by the connector.

A previously announced attachment still owes its detach notice after restoration.
"""
from __future__ import annotations

import pytest

from matrix_chat import connector as matrix_connector
from slack_chat import connector as slack_connector
from slack_chat import sessions as slack_sessions
from slack_chat.client import SlackError
from tests.conftest import drain_connectors
from tests.timing_budgets import DRAIN
from tests.connector_fakes import isolated_connectors
from tests.test_matrix_chat import FakeMatrix


@pytest.fixture(autouse=True)
def connector_lifetimes(monkeypatch):
    instances = []
    for cls in (slack_connector.SlackConnector, matrix_connector.MatrixConnector):
        original = cls.__init__
        def track(instance, *args, _init=original, _cls=cls, **kwargs):
            if _cls is matrix_connector.MatrixConnector:
                kwargs.setdefault("matrix", FakeMatrix())
            _init(instance, *args, **kwargs)
            instance._bus = _FakeBus({"seq": 0, "subscribers": {}})
            instances.append(instance)
        monkeypatch.setattr(cls, "__init__", track)
    yield
    drain_connectors(instances, DRAIN, announce=False)



class FakeSlack:
    def __init__(self, *, fail=False):
        self.posted = []
        self.fail = fail

    def post_message(self, channel, thread_ts, text, *, connection_notice=False):
        if self.fail:
            raise SlackError("slack said no")
        self.posted.append(text)
        return f"ts-{len(self.posted)}"


def slack_connector_with(*, attach_ack=True, slack=None):
    config = slack_connector.ConnectorConfig(
        supervisor_url="http://testserver", supervisor_token="tok",
        channel="C1", thread_ts="1699999999.000000", bot_token="xoxb-test",
        attach_ack=attach_ack,
    )
    return slack_connector.SlackConnector(config, slack=slack or FakeSlack())


class TestTheConnectorsOwnLines:
    def test_an_attachment_is_announced_and_its_end_noted(self):
        slack = FakeSlack()
        built = slack_connector_with(slack=slack)

        built._ensure_ready()
        built._announce_attached()
        built._announce_detached()

        assert slack.posted == [slack_connector.ATTACH_ACK, slack_connector.DETACH_NOTE]

    def test_the_ack_is_posted_once_however_many_times_it_is_asked_for(self):
        slack = FakeSlack()
        built = slack_connector_with(slack=slack)
        built._ensure_ready()
        built._announce_attached()
        built._ensure_ready()
        built._announce_attached()
        assert slack.posted == [slack_connector.ATTACH_ACK]

    def test_a_connector_that_never_attached_says_nothing_on_the_way_out(self):
        """A supervisor that never came up leaves a thread that was never
        listened to — and telling it that listening has stopped would be a lie
        about something that never started."""
        slack = FakeSlack()
        built = slack_connector_with(slack=slack)
        built.stop(timeout=DRAIN)
        assert slack.posted == []

    def test_the_ack_is_off_when_something_else_announced_the_thread(self):
        slack = FakeSlack()
        built = slack_connector_with(attach_ack=False, slack=slack)
        built._ensure_ready()
        built._announce_attached()
        assert slack.posted == []

    def test_saved_announcement_still_owes_its_detach_notice(self):
        slack = FakeSlack()
        built = slack_connector_with(attach_ack=False, slack=slack)
        built._ensure_ready()
        built._announced = True
        built._announce_detached()
        assert slack.posted == [slack_connector.DETACH_NOTE]

    def test_an_ack_with_unknown_outcome_requires_recovery(self):
        from lmer_cli.connector_delivery import RecoveryRequired

        built = slack_connector_with(slack=FakeSlack(fail=True))
        built._ensure_ready()
        with pytest.raises(RecoveryRequired, match="uncertain"):
            built._announce_attached()
        assert built._state.blocked
        assert built._announced is False

    def test_the_env_switch_reaches_the_config(self):
        env = {
            "LMER_SUPERVISOR_URL": "http://127.0.0.1:9000",
            "LMER_FASTAPI_TOKEN": "tok",
            "SLACK_BOT_TOKEN": "xoxb-test",
            "LMER_SLACK_CHANNEL": "C1",
            "LMER_SLACK_THREAD_TS": "1699999999.000000",
        }
        assert slack_connector.ConnectorConfig.from_env(env).attach_ack is True
        off = dict(env, LMER_CHAT_ATTACH_ACK="0")
        assert slack_connector.ConnectorConfig.from_env(off).attach_ack is False
        # The flag on the entrypoint wins over the environment either way: it is
        # the more specific statement about this one process.
        assert slack_connector.ConnectorConfig.from_env(off, attach_ack=True).attach_ack is True


class TestBothTransportsSayTheSameThing:
    def test_matrix_uses_the_slack_wording(self):
        """One definition, imported — two transports drifting apart on what
        "connected" reads like is a difference nobody would notice until a
        thread had both."""
        assert matrix_connector.ATTACH_ACK is slack_connector.ATTACH_ACK
        assert matrix_connector.DETACH_NOTE is slack_connector.DETACH_NOTE

    def test_matrix_announces_through_its_own_thread_note(self):
        config = matrix_connector.ConnectorConfig(
            supervisor_url="http://testserver", supervisor_token="tok",
            homeserver="https://hs.example.org", user_id="@bot:example.org",
            access_token="syt_secret", room="!room:example.org", thread_event="$root",
        )
        built = matrix_connector.MatrixConnector(config)
        built._ensure_ready()
        built._announce_attached()
        built._announce_detached()

        assert [item[2] for item in built.matrix.sent] == [matrix_connector.ATTACH_ACK, matrix_connector.DETACH_NOTE]

    def test_matrix_honours_the_same_switch(self):
        env = {
            "LMER_SUPERVISOR_URL": "http://127.0.0.1:9000",
            "LMER_FASTAPI_TOKEN": "tok",
            "LMER_MATRIX_HOMESERVER": "https://hs.example.org",
            "LMER_MATRIX_USER_ID": "@bot:example.org",
            "LMER_MATRIX_ACCESS_TOKEN": "syt_secret",
            "LMER_MATRIX_ROOM_ID": "!room:example.org",
            "LMER_MATRIX_THREAD_EVENT": "$root",
        }
        assert matrix_connector.ConnectorConfig.from_env(env).attach_ack is True
        off = dict(env, LMER_CHAT_ATTACH_ACK="0")
        assert matrix_connector.ConnectorConfig.from_env(off).attach_ack is False


class TestTheEntrypoints:
    @pytest.mark.parametrize("module", [slack_connector, matrix_connector])
    def test_the_switch_is_spelled_as_a_flag_too(self, module):
        """A sidecar is configured by flags, not only by an environment whose
        contents it may not control."""
        text = _help_of(module)
        assert "--attach-ack" in text and "--no-attach-ack" in text


def _help_of(module):
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), pytest.raises(SystemExit):
        module.main(["--help"])
    return buffer.getvalue()


class TestTheListenersOwnTheirAck:
    def test_both_listeners_spawn_through_the_manager_that_imposes_it(self):
        """The suppression is one line in one place because both listeners spawn
        through one manager. What that line writes into the child's environment
        is pinned in ``test_slack_sessions.py``, where the spawn's whole env is
        asserted; what is pinned here is that the matrix listener goes through
        the same door, so it cannot quietly grow a second spawn path that skips
        it."""
        from matrix_chat import listener as matrix_listener

        assert matrix_listener.SessionManager is slack_sessions.SessionManager
        assert slack_sessions.ATTACH_ACK_ENV == "LMER_CHAT_ATTACH_ACK"

    def test_the_matrix_listener_names_the_preset_too(self):
        """Parity, and the substance of it: the matrix ack already existed, but a
        session silently getting a listener default preset was as invisible from
        a matrix room as issue #181 found it to be from Slack."""
        import asyncio

        from matrix_chat import listener as matrix_listener

        class Sessions:
            max_sessions = 5

            def touch(self, *a):
                return False

            def at_capacity(self):
                return False

            async def spawn(self, *a, **kw):
                return None

            def default_preset(self, env=None):
                return "review", "LMER_CHAT_PRESET"

            def child_presets(self, env=None):
                return {"review": object()}

        said = []
        config = matrix_listener.ListenerConfig({
            "LMER_MATRIX_HOMESERVER": "https://hs",
            "LMER_MATRIX_USER_ID": "@bot:example.org",
            "LMER_MATRIX_ACCESS_TOKEN": "syt",
            "LMER_MATRIX_ALLOWED_USERS": "@vegu:example.org",
        })
        watcher = matrix_listener.Listener(config, Sessions(), matrix=object())
        watcher._say = lambda room, root, text: _record(said, text)

        asyncio.run(watcher.handle("!room:example.org", {
            "event_id": "$m1", "sender": "@vegu:example.org",
            "content": {"body": "@bot start", "msgtype": "m.text"},
        }))

        assert "`review`" in said[0]


async def _record(sink, text):
    sink.append(text)


class TestWhereAnAttachmentStartsReading:
    """Review finding: a mid-life attach used to post the run's whole journal
    into the thread, one message per record, because the outbound cursor started
    at 0 with no way to seed it. Both answers now come from the supervisor."""

    def build(self, health, *, from_now):
        config = slack_connector.ConnectorConfig(
            supervisor_url="http://testserver", supervisor_token="tok",
            channel="C1", thread_ts="1699999999.000000", bot_token="xoxb-test",
            attach_from_now=from_now,
        )
        built = slack_connector.SlackConnector(config, slack=FakeSlack())
        built._bus = _FakeBus(health)
        return built

    def test_a_connection_made_mid_session_starts_at_the_current_position(self):
        built = self.build({"seq": 412, "subscribers": {}}, from_now=True)
        built._seed_cursor()
        assert built._bus_cursor == 412

    def test_a_connection_that_exists_for_the_whole_session_starts_at_the_beginning(self):
        """A launch target is meant to have seen all of it — and this is the CLI
        path, which must behave exactly as it did before."""
        built = self.build({"seq": 412, "subscribers": {}}, from_now=False)
        built._seed_cursor()
        assert built._bus_cursor == 0

    def test_a_connector_that_died_resumes_where_it_was_acked(self):
        """Its own high-water mark outranks both, so a connector that crashed
        mid-session neither replays nor skips."""
        built = self.build({"seq": 412, "subscribers": {}}, from_now=True)
        built._bus.health_body["subscribers"][built.subscriber] = 400
        built._seed_cursor()
        assert built._bus_cursor == 400

    def test_a_remembered_position_is_used_when_the_supervisor_has_forgotten(self):
        """A clean stop acks ``final`` and the supervisor drops the name, so this
        is all that is left of where a graceful restart got to."""
        config = slack_connector.ConnectorConfig(
            supervisor_url="http://testserver", supervisor_token="tok",
            channel="C1", thread_ts="1699999999.000000", bot_token="xoxb-test",
            attach_from_now=False, attach_since=3,
        )
        built = slack_connector.SlackConnector(config, slack=FakeSlack())
        built._bus = _FakeBus({"seq": 6, "subscribers": {}})
        built._seed_cursor()
        assert built._bus_cursor == 3, "not 0, which would re-post the whole journal"

    def test_the_supervisors_own_answer_still_wins(self):
        """It is fresher: the remembered one is written a tick at a time."""
        config = slack_connector.ConnectorConfig(
            supervisor_url="http://testserver", supervisor_token="tok",
            channel="C1", thread_ts="1699999999.000000", bot_token="xoxb-test",
            attach_since=3,
        )
        built = slack_connector.SlackConnector(config, slack=FakeSlack())
        built._bus = _FakeBus({"seq": 6, "subscribers": {built.subscriber: 5}})
        built._seed_cursor()
        assert built._bus_cursor == 5

    def test_a_supervisor_that_will_not_answer_leaves_the_cursor_alone(self):
        from ask_channel.bus import BusError
        built = self.build(None, from_now=True)
        with pytest.raises(BusError):
            built._seed_cursor()
        assert built._bus_cursor == 0

    def test_matrix_reads_the_same_two_facts(self):
        config = matrix_connector.ConnectorConfig(
            supervisor_url="http://testserver", supervisor_token="tok",
            homeserver="https://hs.example.org", user_id="@bot:example.org",
            access_token="syt_secret", room="!room:example.org", thread_event="$root",
            attach_from_now=True,
        )
        built = matrix_connector.MatrixConnector(config)
        built._bus = _FakeBus({"seq": 77, "subscribers": {}})
        built._seed_cursor()
        assert built._bus_cursor == 77

    def test_the_switch_reaches_both_configs_from_the_environment(self):
        env = {
            "LMER_SUPERVISOR_URL": "http://127.0.0.1:9000",
            "LMER_FASTAPI_TOKEN": "tok",
            "SLACK_BOT_TOKEN": "xoxb-test",
            "LMER_SLACK_CHANNEL": "C1",
            "LMER_SLACK_THREAD_TS": "1699999999.000000",
        }
        assert slack_connector.ConnectorConfig.from_env(env).attach_from_now is False
        on = dict(env, LMER_CHAT_ATTACH_FROM_NOW="1")
        assert slack_connector.ConnectorConfig.from_env(on).attach_from_now is True
        assert slack_connector.ConnectorConfig.from_env(env).attach_since is None
        since = dict(env, LMER_CHAT_ATTACH_SINCE="12")
        assert slack_connector.ConnectorConfig.from_env(since).attach_since == 12
        # Anything that is not a count is no answer at all, not a zero.
        assert slack_connector.ConnectorConfig.from_env(
            dict(env, LMER_CHAT_ATTACH_SINCE="soon")
        ).attach_since is None


class TestAConnectorWhoseBusIsGone:
    """Review finding: both loops break when the session ends, but nothing set
    ``_stop``, so ``run_forever`` spun on with no workers — harmless on a thread
    of an exiting process, an immortal orphan as a detached one, whose live pid
    keeps the thread claimed against every future session."""

    def test_the_process_ends_rather_than_spinning(self):
        from ask_channel import bus as bus_mod

        built = slack_connector_with()
        built._bus = _GoneBus()
        built._outbound_loop()
        assert built._stop.is_set()
        assert isinstance(_GoneBus.raised, bus_mod.NoChannel)


class _FakeBus:
    def __init__(self, health_body):
        self.health_body = health_body

    def health(self):
        from ask_channel import bus as bus_mod

        if self.health_body is None:
            raise bus_mod.BusError("supervisor said no")
        return {"session": "notice-session", **self.health_body}


class _GoneBus:
    raised = None

    def health(self):
        return {"session": "gone-session", "seq": 0, "subscribers": {}}

    def messages(self, since, timeout=None):
        from ask_channel import bus as bus_mod

        _GoneBus.raised = bus_mod.NoChannel("the session is over")
        raise _GoneBus.raised
