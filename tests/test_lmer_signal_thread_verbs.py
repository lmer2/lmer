"""`lmer-signal read`, `post --thread` and `thread …` end to end (#414, #429).

The whole path an agent's shell takes for a thread: verb → the supervisor's
request queue → a real :class:`ThreadServicer` on the host side of the same
control plane → stdout and exit code. Slack itself is a fake; everything between
the CLI and it is the real thing.

The ``thread`` verbs stop one step short of that: what the servicer hands them is
the daemon's connections object, which needs a platform state directory and a
registry, and is exercised against the real one in
tests/test_platform_thread_routes.py. Here it is a stand-in, so what these pin is
the CLI's own half — which verb becomes which request, and what a run reads on
stdout afterwards.
"""
from __future__ import annotations

import json
import shlex
import time
from unittest.mock import Mock

import pytest

from ask_channel import bus, signal_cli
from lmer_cli import comms, supervisor, thread_access
from lmer_cli.thread_access import ThreadServicer, UploadStore
from tests.conftest import strip_lmer_env
from tests.connector_fakes import isolated_connectors
from tests.test_lmer_thread_access import SLACK_ENV, SLACK_LINK, FakeSlack


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


class _Session:
    """A ``requests.Session`` stand-in over the FastAPI test client, which
    accepts no ``timeout`` (the bus client always passes one)."""

    def __init__(self, http):
        self._http = http

    def request(self, method, url, **kwargs):
        kwargs.pop("timeout", None)
        return self._http.request(method, url, **kwargs)


class Rig:
    """A supervisor with a bus, the CLI in front of it and a servicer behind."""

    def __init__(self, tmp_path, monkeypatch, *, slack=None, store=None):
        from fastapi.testclient import TestClient

        self.log = comms.MessageLog("sess-1", str(tmp_path / "messages.jsonl"))
        buf = supervisor.OutputBuffer(limit=1024)
        self.app = supervisor._build_fastapi_app(
            buf, lambda data: len(data), "control-token", comms=self.log,
            agent_token="agent-token",
        )
        self.http = TestClient(self.app)
        self.client = bus.BusClient(
            bus.Endpoint(url="http://testserver", token="agent-token"),
            session=_Session(self.http),
        )
        monkeypatch.setattr(signal_cli, "_client", lambda: self.client)
        self.slack = slack if slack is not None else FakeSlack(messages=[
            {"ts": "1712345678.000900", "user": "U1", "text": "what did we decide?"},
        ])
        monkeypatch.setattr(thread_access, "_slack_client", lambda token: self.slack)
        self.servicer = None
        self.store = store

    def serve(self, label="the unified comms run", connections=None, env=None):
        self.servicer = ThreadServicer(
            supervisor_url="http://testserver", token="control-token",
            label=label, env=SLACK_ENV if env is None else env, store=self.store, connections=connections,
            # A short poll so a test's teardown is not the length of a real
            # long-poll; the loop itself is the production one.
            poll_seconds=0.2,
        )
        self.servicer._client = bus.BusClient(
            bus.Endpoint(url="http://testserver", token="control-token"),
            session=_Session(self.http),
        )
        self.servicer.start()
        # Wait for its first poll: "attached" means polling, and the supervisor
        # answers a request with no servicer at once rather than holding it.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            health = self.http.get(
                "/healthz", headers={"Authorization": "Bearer control-token"}
            ).json()
            if health["threads"]["servicer"]:
                break
            time.sleep(0.01)
        return self.servicer

    def stop(self):
        if self.servicer is not None:
            self.servicer.stop()

    def records(self):
        return self.log.read_since(0)[0]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    yield rig
    rig.stop()


def run(argv):
    return signal_cli.main(argv)


class TestRead:
    def test_large_notice_carries_a_command_the_session_can_run(self, rig, capsys):
        from slack_chat.connector import post_replay_notice

        sink = Mock()
        post_replay_notice(
            sink, channel="slack", label="the thread", permalink=SLACK_LINK,
            count=347, oldest="2026-09-01T00:00:00Z", newest="2026-09-08T00:00:00Z", ref="newest",
        )
        text = sink.post.call_args.args[1]
        assert "has 347 earlier messages" in text
        command = text.split("with: ", 1)[1]
        rig.serve()
        assert run(shlex.split(command)[1:]) == 0
        assert "what did we decide?" in capsys.readouterr().out
        assert command.endswith("--limit 200")

    @pytest.mark.parametrize("channel", ["slack", "matrix"])
    def test_read_of_own_connected_thread_leaves_connection_and_cursor_alone(
        self, tmp_path, monkeypatch, capsys, channel,
    ):
        from tests.test_slack_connector import FakeSlack as ConnectorSlack, PNG_FILE, _StreamedPNG
        from tests.test_matrix_chat import FakeMatrix, ROOM, ROOT, BOT
        from tests.test_lmer_thread_access import MATRIX_ENV
        from slack_chat.connector import ConnectorConfig as SlackConfig, SlackConnector
        from matrix_chat.connector import ConnectorConfig as MatrixConfig, MatrixConnector
        from matrix_chat.permalink import matrix_to_link

        store = tmp_path / "uploads"
        store.mkdir()
        transport = ConnectorSlack() if channel == "slack" else FakeMatrix()
        rig = Rig(tmp_path, monkeypatch, slack=transport, store=UploadStore(str(store), "/uploads"))
        control = bus.BusClient(bus.Endpoint(url="http://testserver", token="control-token"), session=_Session(rig.http))
        common = dict(supervisor_url="http://testserver", supervisor_token="control-token",
                      uploads_dir=str(store), container_uploads_dir="/uploads", attach_ack=False)
        if channel == "slack":
            transport.bot_token = "xoxb-test"
            transport.human_says("historical reply", files=[PNG_FILE])
            download = Mock(return_value=_StreamedPNG())
            monkeypatch.setattr(thread_access.requests, "get", download)
            conn = SlackConnector(SlackConfig(**common, channel="C0123ABCDEF",
                                  thread_ts="1712345678.000900", bot_token="xoxb-test"),
                                  slack=transport, bus_client=control)
            link, env = SLACK_LINK, SLACK_ENV
            attach = lambda: conn._poll_thread(replay=True)
            cursor = lambda: conn._cursor_ts
            posted = transport.posted
        else:
            transport.human_says("historical reply", live=False, msgtype="m.file", url="mxc://example.org/file")
            download = Mock(return_value=(b"file data", "text/plain"))
            monkeypatch.setattr(transport, "download", download)
            monkeypatch.setattr(transport, "joined_rooms", lambda: [ROOM], raising=False)
            monkeypatch.setattr(thread_access, "_matrix_client", lambda account: transport)
            conn = MatrixConnector(MatrixConfig(**common, homeserver="https://hs.example.org", user_id=BOT,
                                   access_token="syt-test", room=ROOM, thread_event=ROOT),
                                   matrix=transport, bus_client=control)
            link, env = matrix_to_link(ROOM, ROOT), MATRIX_ENV
            attach = conn.replay
            cursor = lambda: (conn._next_batch, set(conn._seen), conn._last_event_id)
            posted = transport.sent
        connections = Mock()
        rig.serve(connections=connections, env=env)
        try:
            assert attach() == 1
            download.assert_not_called()
            before_cursor, before_seq = cursor(), rig.log.seq
            assert run(["read", link, "--limit", "2", "--json"]) == 0
            records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
            assert len(records) == 2
            assert any(record["text"] == "historical reply" for record in records)
            assert download.call_count == 1 and len(list(store.iterdir())) == 1
            assert cursor() == before_cursor and rig.log.seq == before_seq
            assert posted == [] and connections.mock_calls == []
        finally:
            conn.stop()
            rig.stop()

    def test_a_thread_reads_as_the_records_the_harness_would_have_seen(self, rig, capsys):
        rig.serve()
        assert run(["read", SLACK_LINK]) == 0
        assert capsys.readouterr().out.strip() == "[slack · name-of-U1] what did we decide?"

    def test_json_prints_one_record_per_line(self, rig, capsys):
        rig.serve()
        assert run(["read", SLACK_LINK, "--json"]) == 0
        record = json.loads(capsys.readouterr().out.strip())
        assert record["channel"] == "slack" and record["ref"].startswith("C0123ABCDEF:")

    def test_a_read_leaves_no_record_on_this_sessions_own_conversation(self, rig):
        rig.serve()
        run(["read", SLACK_LINK])
        assert rig.records() == []

    def test_without_a_servicer_the_session_is_told_at_once_and_exits_three(self, rig, capsys):
        assert run(["read", SLACK_LINK]) == signal_cli.NO_CHANNEL_EXIT_CODE
        assert "no host-side servicer" in capsys.readouterr().err

    def test_a_refusal_from_the_host_side_is_an_error_not_a_timeout(self, tmp_path, monkeypatch, capsys):
        rig = Rig(tmp_path, monkeypatch, slack=FakeSlack(fail="Slack API error: not_in_channel", code="not_in_channel"))
        rig.serve()
        try:
            assert run(["read", SLACK_LINK, "--timeout", "20"]) == 1
        finally:
            rig.stop()
        assert "invite it" in capsys.readouterr().err


class TestPostIntoAThread:
    def test_the_post_is_attributed_and_recorded_on_this_sessions_bus(self, rig, capsys):
        rig.serve()
        assert run(["post", "the review is posted", "--thread", SLACK_LINK]) == 0
        out = capsys.readouterr()
        assert out.out.strip() == "000001"
        assert rig.slack.posted[0][2] == "[the unified comms run] the review is posted"
        record = rig.records()[0]
        assert record.kind == "message" and record.text == "the review is posted"
        # Where it went, on the record: the pane and the run's connected threads
        # read it from here.
        assert record.meta["thread"].startswith("https://20c.slack.com/archives/C0123ABCDEF/")
        assert "posted into" in out.err

    def test_a_post_that_never_landed_leaves_no_record_claiming_it_did(
        self, tmp_path, monkeypatch, capsys
    ):
        rig = Rig(tmp_path, monkeypatch, slack=FakeSlack(fail="Slack API error: channel_not_found", code="channel_not_found"))
        rig.serve()
        try:
            assert run(["post", "the review is posted", "--thread", SLACK_LINK, "--timeout", "20"]) == 1
        finally:
            rig.stop()
        assert rig.records() == []

    def test_a_record_that_fails_after_delivery_says_do_not_retry(self, rig, capsys, monkeypatch):
        """The one order this verb can fail in that an agent would make worse.

        The message is already in a thread this run does not own; an agent told
        "failed" retries, and the stranger's thread gets it twice — the exact
        outcome the claim-once queue exists to prevent one layer down."""
        rig.serve()
        real_post = rig.client.post

        def refuse(kind, text, **kw):
            if kw.get("meta", {}).get("thread"):
                raise bus.BusError("the supervisor refused the record")
            return real_post(kind, text, **kw)

        monkeypatch.setattr(rig.client, "post", refuse)
        assert run(["post", "the review is posted", "--thread", SLACK_LINK]) == 1
        err = capsys.readouterr().err
        assert "do not send it again" in err
        # And the receipt is there, before the failure: it is the only evidence
        # that anything landed.
        assert "posted into" in err
        assert rig.slack.posted[0][2].endswith("the review is posted")

    def test_a_post_too_long_for_one_message_says_it_was_clipped(self, rig, capsys):
        """The record keeps the whole text and the thread gets what it takes; a
        caller told "posted" with no more than that could not find out."""
        rig.serve()
        assert run(["post", "x" * 5000, "--thread", SLACK_LINK]) == 0
        assert "clipped" in capsys.readouterr().err
        assert len(rig.slack.posted[0][2]) <= thread_access.MAX_POST_CHARS
        assert len(rig.records()[0].text) == 5000

    def test_a_post_without_a_thread_is_the_conversation_it_always_was(self, rig, capsys):
        assert run(["post", "morning"]) == 0
        record = rig.records()[0]
        assert record.text == "morning" and "thread" not in record.meta
        assert rig.slack.posted == []

    def test_an_attachment_rides_along_and_is_named_in_the_thread(
        self, tmp_path, monkeypatch, capsys
    ):
        uploads = tmp_path / "uploads"
        uploads.mkdir()
        (tmp_path / "review.md").write_text("two majors")
        monkeypatch.setenv("LMER_UPLOADS_DIR", str(uploads))
        rig = Rig(
            tmp_path, monkeypatch,
            store=UploadStore(host_dir=str(uploads), container_dir=str(uploads)),
        )
        rig.serve()
        try:
            assert run([
                "post", "the review", "--thread", SLACK_LINK,
                "--attach", str(tmp_path / "review.md"),
            ]) == 0
        finally:
            rig.stop()
        assert "📎 review.md" in rig.slack.posted[0][2]
        assert rig.records()[0].attachments[0]["name"] == "review.md"


class _Connections:
    """The daemon's connections object, as the servicer sees it."""

    def __init__(self):
        self.calls = []

    def open(self, channel, *, text="", poster=None):
        # `poster` is the servicer's own (#427): the root message is a post this
        # host made, and it carries the same attribution as any other.
        self.calls.append(("open", channel, text, poster))
        return {"thread": {
            "id": "slack:9f2c1d", "channel": "slack", "label": "20c · #a-channel",
            "permalink": "https://20c.slack.com/archives/C0123ABCDEF/p1712345678000900",
        }}

    def connect(self, permalink):
        self.calls.append(("connect", permalink))
        return {"thread": {"id": "slack:9f2c1d", "label": "20c · #a-channel",
                           "permalink": permalink}}

    def disconnect(self, permalink):
        self.calls.append(("disconnect", permalink))
        return {"thread": {"id": "slack:9f2c1d", "label": "20c · #a-channel",
                           "permalink": permalink}}


class TestTheRunsOwnThreads:
    def test_open_prints_the_permalink_on_stdout_alone(self, rig, capsys):
        connections = _Connections()
        rig.serve(connections=connections)

        assert run(["thread", "open", "#a-channel", "watching the release"]) == 0

        out = capsys.readouterr()
        assert out.out.strip() == "https://20c.slack.com/archives/C0123ABCDEF/p1712345678000900"
        assert "opened a thread in 20c · #a-channel" in out.err
        # And the poster is the real one: this rig has a live supervisor, so the
        # servicer answers "which session am I serving" from its `/healthz` — the
        # same place the connector comparing against it asks (#427).
        verb, channel, said, poster = connections.calls[0]
        assert (verb, channel, said) == ("open", "#a-channel", "watching the release")
        assert poster.is_session("sess-1")

    def test_connect_and_disconnect_carry_the_permalink_the_run_holds(self, rig, capsys):
        connections = _Connections()
        rig.serve(connections=connections)

        assert run(["thread", "connect", SLACK_LINK]) == 0
        assert run(["thread", "disconnect", SLACK_LINK]) == 0

        assert connections.calls == [("connect", SLACK_LINK), ("disconnect", SLACK_LINK)]
        assert "disconnected 20c · #a-channel" in capsys.readouterr().err

    def test_a_host_side_that_keeps_no_connections_says_so_and_exits_one(self, rig, capsys):
        """A shell `lmer` session: its threads are launch arguments, and the
        refusal has to say that rather than read as a broken channel."""
        rig.serve()
        assert run(["thread", "open", "#a-channel", "--timeout", "20"]) == 1
        assert "launch arguments" in capsys.readouterr().err

    def test_a_channel_that_names_no_transport_never_reaches_the_host(self, rig, capsys):
        connections = _Connections()
        rig.serve(connections=connections)
        assert run(["thread", "open", "not a channel", "--timeout", "20"]) == 1
        assert connections.calls == []

    def test_a_timed_out_open_is_indeterminate_and_not_a_failure(self, rig, capsys):
        """Review finding on !299 (both fan-out reviewers): the queue stops a
        request being *serviced* twice, not asked twice. The host may finish
        after this process stops waiting, so an agent told "it failed" retries
        and puts a second root message in a human's channel."""
        rig.serve(connections=_Connections())
        # Attached, then gone: the queue takes the request and nothing is left
        # to answer it, which is the shape a real servicer death has.
        rig.servicer.stop()

        assert run(["thread", "open", "#a-channel", "--timeout", "0"]) == 1

        err = capsys.readouterr().err
        assert "INDETERMINATE, not failed" in err
        assert "may already exist in #a-channel" in err
        assert "second root message" in err
        # And a way to find out, which is a verb the run already has.
        assert "thread connect" in err

    def test_a_timed_out_read_still_reads_as_the_host_being_gone(self, rig, capsys):
        """The generic message is unchanged where it was right: nothing is
        created, so giving up costs nothing."""
        rig.serve()
        rig.servicer.stop()
        assert run(["read", SLACK_LINK, "--timeout", "0"]) == 1
        err = capsys.readouterr().err
        assert "it may be gone" in err
        assert "INDETERMINATE" not in err

    def test_the_verb_without_one_of_the_three_says_which_three(self, rig, capsys):
        assert run(["thread"]) == 1
        assert "open, connect or disconnect" in capsys.readouterr().err
