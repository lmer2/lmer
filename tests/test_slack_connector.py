"""The Slack connector (issue #407, T5): one thread mirrored onto one bus.

Both directions run against a real supervisor bus (the FastAPI routes of
``lmer_cli.supervisor`` over a real ``MessageLog``) through a fake Slack
client, so what is pinned is the connector's own behaviour: what it posts to
the thread for each kind of record, what it puts on the bus for a human
reply, how a reply becomes an answer, and that its own posts never come back
as messages.
"""
from __future__ import annotations

import html
import threading
import time
from dataclasses import replace

import pytest

from ask_channel import bus
from lmer_cli import comms, supervisor, targets, thread_posts
from slack_chat import connector, registry
from slack_chat.client import SlackError
from tests.conftest import drain_connectors, strip_lmer_env
from tests.timing_budgets import CONNECT_EXPIRED, DRAIN, JOIN, QUIET, outlast
from tests.test_lmer_signal_cli import _Session


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture(autouse=True)
def registry_dir(tmp_path, monkeypatch):
    """Where the host records which run posted a message into a thread (#427)."""
    monkeypatch.setattr(thread_posts, "REGISTRY_DIR", str(tmp_path / "thread-posts"))
    monkeypatch.setattr(registry, "REGISTRY_DIR", str(tmp_path / "thread-owners"))
    instances = []
    original_init = connector.SlackConnector.__init__

    def tracked_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._bus_poll_seconds = 0.05
        instances.append(self)

    monkeypatch.setattr(connector.SlackConnector, "__init__", tracked_init)
    yield
    drain_connectors(instances, DRAIN)


class FakeSlack:
    """The three Slack calls the connector makes, recorded."""

    def __init__(self):
        self.posted: list = []
        self.replies: list = []
        self.bot_user_id = "UBOT"
        self.fail_post = False
        self.attempts = 0
        self.reactions = []
        self.names = {"U1": {"display_name": "tester"}}

    def workspace(self):
        return {"host": "example.slack.com"}

    def add_reaction(self, channel, timestamp, name):
        reaction = (channel, timestamp, name)
        if reaction not in self.reactions:
            self.reactions.append(reaction)

    def auth_test(self):
        return self.bot_user_id

    def post_message(self, channel, thread_ts, text, *, connection_notice=False):
        self.attempts += 1
        if self.fail_post:
            raise SlackError("rate limited", code="ratelimited")
        ts = f"{1700000000 + len(self.posted)}.000100"
        self.posted.append((channel, thread_ts, text))
        # The bot's own post shows up in the thread, like the real thing.
        self.replies.append({"ts": ts, "user": self.bot_user_id, "text": text})
        if connection_notice:
            self.replies[-1]["blocks"] = [{"block_id": connector.CONNECTION_NOTICE_BLOCK_PREFIX + ts}]
        return ts

    def get_replies(self, channel, thread_ts, oldest=None):
        parent = {"ts": thread_ts, "user": "U1", "text": "@lmer help me"}
        found = [parent] + [m for m in self.replies if oldest is None or m["ts"] > oldest]
        return found

    def _request(self, method, endpoint, **kwargs):
        if endpoint == "users.info":
            user = kwargs["params"]["user"]
            if user not in self.names:
                raise SlackError("user_not_found")
            return {"ok": True, "user": {"profile": self.names[user], "name": user}}
        raise SlackError(f"unexpected {endpoint}")

    def app_says(self, text, *, ts=None):
        """A message by the app every lmer session shares — what another run's
        `post --thread` and the platform's own post verb both leave behind."""
        ts = ts or f"{1700001000 + len(self.replies)}.000300"
        self.replies.append({
            "ts": ts, "user": self.bot_user_id, "bot_id": "B0LMER",
            "app_id": "A0LMER", "text": text,
        })
        return ts

    def human_says(self, text, *, user="U1", ts=None, files=None):
        ts = ts or f"{1700001000 + len(self.replies)}.000200"
        message = {"ts": ts, "user": user, "text": text}
        if files:
            message["files"] = files
        self.replies.append(message)
        return ts


class Rig:
    def __init__(self, tmp_path, *, uploads=None, launch_settings=None):
        from fastapi.testclient import TestClient

        self.log = comms.MessageLog("sess-1", str(tmp_path / "messages.jsonl"))
        self.sink: list = []
        buf = supervisor.OutputBuffer(limit=4096)

        def write_input(data):
            self.sink.append(bytes(data))
            return len(data)

        app = supervisor._build_fastapi_app(
            buf, write_input, "tok", comms=self.log, launch_settings=launch_settings,
        )
        http = TestClient(app)
        client = bus.BusClient(bus.Endpoint(url="http://testserver", token="tok"), session=_Session(http))
        self.bus = client
        self.slack = FakeSlack()
        self.config = connector.ConnectorConfig(
            supervisor_url="http://testserver", supervisor_token="tok",
            channel="C1", thread_ts="1699999999.000000", bot_token="xoxb-test",
            poll_seconds=0.05, uploads_dir=uploads, container_uploads_dir="/home/developer/.lmer-uploads",
            # These tests are about what a *record* renders as. The attach ack is
            # a line the connector posts for itself and has its own tests
            # (tests/test_chat_connector_notices.py); leaving it on here would put
            # it at the head of every posted list below.
            attach_ack=False,
        )
        self.connector = connector.SlackConnector(self.config, slack=self.slack, bus_client=client)
        self.connector._bus_poll_seconds = 0.05

    def agent(self, kind, text, **kw):
        return self.log.append(kind=kind, text=text, source=comms.Source("agent"), session_caller=True, **kw)[0]

    def wait_until(self, predicate, timeout=JOIN):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert predicate(), "condition not reached in time"


class _StreamedPNG:
    """A file download as the bounded fetch reads it: streamed, because the
    fetch is bounded as it arrives now — the same helper the `lmer-signal read`
    path uses (issue #414), so the same Slack file is no longer accepted here
    and refused there."""

    headers = {"Content-Type": "image/png"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=65536):
        yield b"\x89PNG\r\n\x1a\n"


def _stub_file_download(monkeypatch):
    from lmer_cli import thread_access

    monkeypatch.setattr(thread_access.requests, "get", lambda *a, **kw: _StreamedPNG())


PNG_FILE = {"name": "shot.png", "mimetype": "image/png", "url_private": "https://files.slack/x"}


@pytest.fixture
def rig(tmp_path):
    rig = Rig(tmp_path)
    yield rig
    rig.connector.stop()
    rig.bus._session._http.close()
    rig.log.close()


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


class TestRendering:
    def _record(self, kind, text, channel="agent", author=None, **extra):
        source = {"channel": channel}
        if author:
            source["author"] = author
        return {"kind": kind, "text": text, "source": source, "meta": {}, **extra}

    def test_a_question_lists_numbered_options(self):
        text = connector.render_outbound(self._record("question", "branch?", options=["main", "prep"]))
        assert text.startswith("❓ branch?\n1. main\n2. prep\n")
        assert "first answer from any channel wins" in text

    def test_answers_say_who_and_from_where(self):
        question = self._record("question", "branch?", id="q1")
        answer = self._record("answer", "prep", channel="matrix", author="@a:x", in_reply_to="q1")
        text = connector.render_outbound(answer, {"q1": question})
        assert text == "✅ answered to “branch?” from matrix · @a:x: prep"
        late = self._record("answer", "main", channel="platform", author="tester", in_reply_to="q1", meta={"late": True})
        assert connector.render_outbound(late, {"q1": question}).startswith("↩️ late answer")

    def test_off_thread_records_are_skipped(self):
        record = self._record("question", "lost?", channel="platform", meta={comms.META_OFF_THREAD: True})
        assert connector.render_outbound(record) is None

    def test_slacks_own_records_are_skipped(self):
        assert connector.render_outbound(self._record("message", "hi", channel="slack", author="tester")) is None

    def test_the_agents_message_is_plain_and_other_channels_are_tagged(self):
        assert connector.render_outbound(self._record("message", "done")) == "done"
        assert connector.render_outbound(self._record("message", "go", channel="uber-lmer")) == "💬 [uber lmer] go"
        assert connector.render_outbound(self._record("message", "ok", channel="terminal")) == "💬 [terminal] ok"
        assert connector.render_outbound(self._record("note", "working")) is None
        assert connector.render_outbound(self._record("milestone", "pushed")) == "🏁 pushed"
        assert connector.render_outbound(self._record("command", "/followup", channel="uber-lmer")) == "⌨️ [uber lmer] /followup"

    def test_attachments_are_named(self):
        text = connector.render_outbound(self._record("message", "see", attachments=[{"name": "shot.png"}]))
        assert text == "see\n📎 shot.png"

    @pytest.mark.parametrize("reply, expected", [
        ("2", "prep"), ("2.", "prep"), (" 1 ", "main"), ("PREP", "prep"), ("main", "main"),
        ("3", None), ("something else", None),
    ])
    def test_option_for(self, reply, expected):
        assert connector.option_for(reply, ["main", "prep"]) == expected

    @pytest.mark.parametrize("value, expected", [
        (0, "1970-01-01T00:00:00Z"),
        (1699999999.0, "2023-11-14T22:13:19Z"),
        (None, "time unknown"),
        (float("nan"), "time unknown"),
        (float("inf"), "time unknown"),
        (float("-inf"), "time unknown"),
        (1e20, "time unknown"),
        (10 ** 18 / 1000, "time unknown"),
    ])
    def test_replay_stamp_answers_every_value_rather_than_raising(self, value, expected):
        """Untrusted timestamps remain printable in replay notices."""
        assert connector.replay_stamp(value) == expected

    def test_option_for_without_options(self):
        assert connector.option_for("1", []) is None


# ---------------------------------------------------------------------------
# outbound
# ---------------------------------------------------------------------------


class TestOutbound:
    def test_peer_ul_records_never_post_but_worker_messages_still_do(self, rig):
        record = {"id": "sess-1/peer", "kind": "message", "text": "Private native coordination",
                  "source": {"channel": "peer-ul", "author": "Example Alpha"}, "meta": {}}
        rig.connector._send_record(record)
        assert rig.slack.posted == []
        rig.connector._send_record({**record, "id": "sess-1/worker", "source": {"channel": "uber-lmer"}})
        assert len(rig.slack.posted) == 1

    def test_agent_records_reach_the_thread_and_the_cursor_is_acked(self, rig):
        rig.connector.start()
        rig.agent("note", "working on it")
        rig.agent("question", "branch?", options=["main", "prep"])
        rig.wait_until(lambda: len(rig.slack.posted) >= 1)
        assert rig.slack.posted[0][2].startswith("❓ branch?\n1. main\n2. prep")
        assert rig.slack.posted[0][:2] == ("C1", "1699999999.000000")
        # The inbound loop also replayed the thread root onto the bus, so the
        # acked position is whatever the log has grown to, not a fixed number.
        rig.wait_until(lambda: rig.log.subscribers().get(rig.connector.subscriber) == rig.log.seq)

    def test_a_slack_record_is_not_posted_back(self, rig):
        rig.connector.start()
        rig.log.append(kind="message", text="from slack", source=comms.Source("slack", author="tester", ref="t9"))
        rig.agent("message", "after")
        rig.wait_until(lambda: len(rig.slack.posted) >= 1)
        assert [p[2] for p in rig.slack.posted] == ["after"]

    def test_a_verified_rejection_is_held_and_retried(self, rig):
        """A structured rejection establishes that Slack did not accept the post."""
        rig.slack.fail_post = True
        rig.connector.start()
        rig.agent("message", "one")
        rig.wait_until(lambda: rig.slack.attempts >= 1)
        assert rig.log.subscribers().get(rig.connector.subscriber) in (None, 0), "nothing was delivered to ack"
        rig.slack.fail_post = False
        rig.agent("message", "two")
        rig.wait_until(lambda: len(rig.slack.posted) == 2)
        assert [text for _, _, text in rig.slack.posted] == ["one", "two"]
        rig.wait_until(lambda: rig.log.subscribers().get(rig.connector.subscriber) == rig.log.seq)


# ---------------------------------------------------------------------------
# inbound
# ---------------------------------------------------------------------------


class TestInbound:
    @pytest.mark.parametrize("with_replies", [False, True])
    def test_default_attach_and_restart_exclude_lifecycle_notices(self, rig, monkeypatch, with_replies):
        if with_replies:
            rig.slack.human_says(connector.ATTACH_ACK)
            rig.slack.app_says("a previous session reply")
        config = replace(rig.config, attach_ack=True)
        expected = 3 if with_replies else 1
        post = rig.slack.post_message

        def store_post(*args, **kwargs):
            newest = max(float(m["ts"]) for m in rig.slack.get_replies("C1", rig.config.thread_ts))
            post(*args, **kwargs)
            ts = f"{newest + 1:.6f}"
            rig.slack.replies[-1]["ts"] = ts
            return ts

        monkeypatch.setattr(rig.slack, "post_message", store_post)
        for _ in range(2):
            current = connector.SlackConnector(config, slack=rig.slack, bus_client=rig.bus)
            current.start()
            try:
                rig.wait_until(lambda: current._cursor_ts is not None)
                (notice,) = rig.log.read_since(0)[0]
                assert f"has {expected} earlier message" in notice.text
                assert notice.source.ref.endswith(
                    rig.slack.replies[1]["ts"] if with_replies else rig.config.thread_ts
                )
            finally:
                current.stop()

    def test_attach_is_a_notice_without_body_rendering(self, rig, monkeypatch):
        for index in range(25):
            rig.slack.human_says(f"stale request {index}", files=[PNG_FILE])
        rig.slack.app_says("previous session response")
        monkeypatch.setattr(rig.connector, "_inbound_body", lambda message: pytest.fail("attach rendered a message body"))
        assert rig.connector._poll_thread(replay=True) == 1
        (record,) = rig.log.read_since(0)[0]
        assert len(record.text.splitlines()) == 1
        assert "has 27 earlier messages" in record.text
        assert "lmer-signal read" in record.text and "--limit 27" in record.text
        assert "stale request" not in record.text
        assert "previous session response" not in record.text
        assert not record.attachments

    def test_the_thread_root_and_new_human_replies_become_messages(self, rig):
        posted = rig.connector._poll_thread(replay=True)
        assert posted == 1
        (record,) = rig.log.read_since(0)[0]
        assert record.kind == "message"
        assert "has 1 earlier message (2023-11-14T22:13:19Z .. 2023-11-14T22:13:19Z)" in record.text
        assert record.source == comms.Source("slack", ref="C1:1699999999.000000:1699999999.000000")
        assert b"[slack] [replay notice]" in b"".join(rig.sink)
        rig.slack.human_says("please rebuild", user="U2")
        assert rig.connector._poll_thread() == 1
        second = rig.log.read_since(1)[0][0]
        assert second.source.author == "U2", "an unknown user falls back to the id"
        assert rig.connector._poll_thread() == 0, "nothing new is posted twice"

    def test_the_bots_own_posts_are_never_ingested(self, rig):
        rig.connector._poll_thread(replay=True)
        rig.slack.post_message("C1", "1699999999.000000", "📝 hello from the bot")
        assert rig.connector._poll_thread() == 0

    def test_a_reply_while_a_question_is_open_is_its_answer(self, rig):
        rig.connector._poll_thread(replay=True)
        question = rig.agent("question", "branch?", options=["main", "prep-release"])
        rig.slack.human_says("2")
        assert rig.connector._poll_thread() == 1
        answer = rig.log.read_since(question.seq)[0][0]
        assert (answer.kind, answer.text, answer.in_reply_to) == ("answer", "prep-release", question.id)
        assert rig.log.question(str(question.seq)).state == comms.STATE_ANSWERED
        rig.slack.human_says("thanks")
        rig.connector._poll_thread()
        last = rig.log.read_since(answer.seq)[0][-1]
        assert last.kind == "message", "with no open question a reply is a message"

    def test_free_text_answers_the_oldest_open_question(self, rig):
        rig.connector._poll_thread(replay=True)
        first = rig.agent("question", "a?")
        rig.agent("question", "b?")
        rig.slack.human_says("whatever you think")
        rig.connector._poll_thread()
        answer = rig.log.read_since(0)[0][-1]
        assert answer.in_reply_to == first.id and answer.text == "whatever you think"

    def test_free_text_never_answers_an_off_thread_question(self, rig):
        rig.connector._poll_thread(replay=True)
        rig.log.append(kind="question", text="lost?", source=comms.Source("platform"), off_thread=True)
        mine = rig.agent("question", "a?")
        rig.slack.human_says("whatever you think")
        rig.connector._poll_thread()
        answer = rig.log.read_since(0)[0][-1]
        assert answer.in_reply_to == mine.id

    def test_conversation_beside_an_open_question_is_not_its_answer(self, rig):
        """A human in a thread has not promised to be answering. Binding "hold
        on, let me look" as the answer closed the question under first-answer-
        wins, and the agent branched on it (operator's ruling on decision 12)."""
        rig.connector._poll_thread(replay=True)
        question = rig.agent("question", "branch?", options=["main", "prep-release"])
        rig.slack.human_says("hold on, let me look")
        rig.connector._poll_thread()
        last = rig.log.read_since(question.seq)[0][-1]
        assert last.kind == "message" and last.text == "hold on, let me look"
        assert rig.log.question(str(question.seq)).open, "the question stays open"
        rig.slack.human_says("2")
        rig.connector._poll_thread()
        answer = rig.log.read_since(question.seq)[0][-1]
        assert (answer.kind, answer.text) == ("answer", "prep-release")

    def test_only_the_first_message_can_free_text_an_option_less_question(self, rig):
        rig.connector._poll_thread(replay=True)
        question = rig.agent("question", "what should I call it?")
        rig.slack.human_says("call it widget")
        rig.connector._poll_thread()
        first = rig.log.read_since(question.seq)[0][-1]
        assert (first.kind, first.text) == ("answer", "call it widget")
        second_q = rig.agent("question", "and the other one?")
        rig.slack.human_says("ha, nice")
        rig.slack.human_says("no idea really")
        rig.connector._poll_thread()
        records = rig.log.read_since(second_q.seq)[0]
        assert [(r.kind, r.text) for r in records if r.source.channel == "slack"] == [
            ("answer", "ha, nice"), ("message", "no idea really"),
        ]

    def test_a_file_in_a_message_lands_in_the_upload_store(self, tmp_path, monkeypatch):
        store = tmp_path / "uploads"
        store.mkdir()
        rig = Rig(tmp_path, uploads=str(store))
        _stub_file_download(monkeypatch)
        rig.connector._poll_thread(replay=True)
        rig.slack.human_says("look at this", files=[PNG_FILE])
        rig.connector._poll_thread()
        record = rig.log.read_since(0)[0][-1]
        (attachment,) = record.attachments
        assert attachment["name"] == "shot.png" and attachment["media_type"] == "image/png"
        assert attachment["path"].startswith("/home/developer/.lmer-uploads/")
        assert "[lmer upload] /home/developer/.lmer-uploads/" in record.text
        assert len(list(store.iterdir())) == 1
        rig.connector.stop()


class TestAttachNotice:
    def test_the_notice_has_the_range_and_link_and_downloads_nothing(self, tmp_path, monkeypatch):
        from unittest.mock import Mock
        from lmer_cli import thread_access

        store = tmp_path / "uploads"
        store.mkdir()
        rig = Rig(tmp_path, uploads=str(store))
        download = Mock(return_value=_StreamedPNG())
        monkeypatch.setattr(thread_access.requests, "get", download)
        rig.slack.human_says("second", user="U2")
        ts = rig.slack.human_says("third", files=[PNG_FILE])
        assert rig.connector._poll_thread(replay=True) == 1
        (record,) = rig.log.read_since(0)[0]
        assert (record.kind, record.in_reply_to) == ("message", None)
        assert record.source == comms.Source("slack", ref=f"C1:1699999999.000000:{ts}")
        assert record.text == (
            "[replay notice] the slack thread C1:1699999999.000000 has 3 earlier messages "
            "(2023-11-14T22:13:19Z .. 2023-11-14T22:30:01Z). Read them with: "
            "lmer-signal read https://example.slack.com/archives/C1/p1699999999000000 --limit 3"
        )
        assert not record.attachments and list(store.iterdir()) == []
        download.assert_not_called()
        rig.slack.human_says("after the attach", files=[PNG_FILE])
        assert rig.connector._poll_thread() == 1
        live = rig.log.read_since(record.seq)[0][-1]
        assert live.text.startswith("after the attach") and live.source.author == "tester"
        assert len(live.attachments) == 1 and download.call_count == 1
        rig.connector.stop()

    def test_a_thread_with_only_a_root_gets_a_notice(self, rig):
        assert rig.connector._poll_thread(replay=True) == 1
        (record,) = rig.log.read_since(0)[0]
        assert "has 1 earlier message" in record.text
        assert len(record.text.splitlines()) == 1
        assert "@lmer help me" not in record.text

    def test_replayed_history_is_never_an_answer_and_leaves_the_free_slot(self, rig):
        """A live connect can land on a session that has already asked something.
        The backlog predates the attach, so it answers nothing — and does not
        spend the first-free-text slot either, so the next real message can."""
        question = rig.agent("question", "what should I call it?")
        rig.slack.human_says("morning")
        assert rig.connector._poll_thread(replay=True) == 1
        replay = rig.log.read_since(question.seq)[0][-1]
        assert (replay.kind, replay.in_reply_to) == ("message", None)
        assert rig.log.question(str(question.seq)).open
        rig.slack.human_says("call it widget")
        assert rig.connector._poll_thread() == 1
        answer = rig.log.read_since(replay.seq)[0][-1]
        assert (answer.kind, answer.text, answer.in_reply_to) == (
            "answer", "call it widget", question.id,
        )

    def test_a_bot_only_thread_still_gets_a_notice(self, rig, monkeypatch):
        monkeypatch.setattr(rig.slack, "get_replies", lambda *args, **kwargs: [
            {"ts": rig.config.thread_ts, "bot_id": "B1", "text": "posted by an app"},
        ])
        assert rig.connector._poll_thread(replay=True) == 1
        (record,) = rig.log.read_since(0)[0]
        assert "has 1 earlier message" in record.text
        assert "posted by an app" not in record.text

    def test_an_empty_snapshot_posts_nothing(self, rig, monkeypatch):
        monkeypatch.setattr(rig.slack, "get_replies", lambda *args, **kwargs: [])
        assert rig.connector._poll_thread(replay=True) == 0
        assert rig.log.seq == 0

    def test_a_file_only_message_supplies_the_newest_ref_without_a_store(self, rig):
        rig.connector._poll_thread(replay=True)
        settled = rig.log.seq
        ts = rig.slack.human_says("", files=[PNG_FILE])
        rig.connector.stop()
        fresh = connector.SlackConnector(rig.config, slack=rig.slack, bus_client=rig.bus)
        rig.connector = fresh
        assert fresh._poll_thread(replay=True) == 1
        (record,) = rig.log.read_since(settled)[0]
        assert record.source.ref.endswith(ts)
        assert "has 2 earlier messages" in record.text

    def test_a_restarted_connector_replays_the_same_thread_only_once(self, rig):
        """The batch takes its ref from the newest message in it, so a connector
        that died and re-read the same thread relays nothing again — while one
        that finds something new replays the snapshot rather than losing it."""
        rig.slack.human_says("second")
        assert rig.connector._poll_thread(replay=True) == 1
        settled = rig.log.seq
        rig.connector.stop()
        fresh = connector.SlackConnector(rig.config, slack=rig.slack, bus_client=rig.connector.bus)
        rig.connector = fresh
        assert fresh._poll_thread(replay=True) == 1, "it posts; the bus is what dedupes"
        assert rig.log.seq == settled, "and appended nothing"
        rig.slack.human_says("while it was down")
        rig.connector.stop()
        again = connector.SlackConnector(rig.config, slack=rig.slack, bus_client=rig.connector.bus)
        rig.connector = again
        assert again._poll_thread(replay=True) == 1
        assert rig.log.seq > settled, "a snapshot with something new in it is not a duplicate"
        assert "has 3 earlier messages" in rig.log.read_since(settled)[0][-1].text


# ---------------------------------------------------------------------------
# configuration and the target handler
# ---------------------------------------------------------------------------


class TestPostsFromOtherRuns:
    """A bot-authored message is not automatically this connector's own echo
    (#427): every lmer session posts through one Slack app, so who wrote it is
    read from what the host recorded, never from the `[lmer session ...]` text.
    """

    #: The `source.ref` shape the bus keeps for this thread, unchanged by #427.
    THREAD = "C1:1699999999.000000"

    def _recorded(self, rig, ts, poster):
        """What the host wrote down when it made the post: keyed by the channel
        and the message's own id, which is the pair both sides agree on."""
        assert thread_posts.record("C1", ts, poster)

    def test_another_runs_post_is_relayed_with_its_run_as_the_author(self, rig):
        rig.connector._poll_thread(replay=True)
        ts = rig.slack.app_says("[lmer session s-other] have a look at !288")
        self._recorded(rig, ts, thread_posts.Poster.for_session("s-other", "chat-mr-288"))
        assert rig.connector._poll_thread() == 1
        record = rig.log.read_since(1)[0][0]
        assert record.kind == "message"
        assert record.source == comms.Source(
            "slack", author="lmer chat-mr-288", ref=f"{self.THREAD}:{ts}",
        )
        assert record.text == "[lmer session s-other] have a look at !288", (
            "the prefix the host composed stays in the body"
        )
        assert b"[slack \xc2\xb7 lmer chat-mr-288]" in b"".join(rig.sink)

    def test_a_run_with_no_title_yet_is_named_by_its_session(self, rig):
        rig.connector._poll_thread(replay=True)
        ts = rig.slack.app_says("still nameless")
        self._recorded(rig, ts, thread_posts.Poster.for_session("s-other"))
        rig.connector._poll_thread()
        assert rig.log.read_since(1)[0][0].source.author == "lmer session s-other"

    def test_the_platforms_own_post_is_relayed_as_the_platform(self, rig):
        rig.connector._poll_thread(replay=True)
        ts = rig.slack.app_says("[operator] look at the pipeline")
        self._recorded(rig, ts, thread_posts.Poster.for_platform("operator"))
        assert rig.connector._poll_thread() == 1
        assert rig.log.read_since(1)[0][0].source.author == "platform \u00b7 operator"
        assert b"[slack \xc2\xb7 platform \xc2\xb7 operator]" in b"".join(rig.sink)

    def test_this_sessions_own_post_through_its_servicer_is_still_skipped(self, rig):
        rig.connector._poll_thread(replay=True)
        ts = rig.slack.app_says("[lmer session sess-1] posted into another thread")
        self._recorded(rig, ts, thread_posts.Poster.for_session("sess-1", "this run"))
        assert rig.connector._poll_thread() == 0

    def test_a_post_the_host_recorded_for_nobody_is_skipped(self, rig):
        """Own-echo skip is the default, so the connector's own mirror, the
        listener's acks and any other app in the thread stay out."""
        rig.connector._poll_thread(replay=True)
        rig.slack.app_says("[lmer session s-other] a text prefix is not evidence")
        assert rig.connector._poll_thread() == 0
        assert rig.log.read_since(1)[0] == []

    def test_a_human_in_the_same_thread_is_untouched(self, rig):
        rig.connector._poll_thread(replay=True)
        ts = rig.slack.app_says("[lmer session s-other] from another run")
        self._recorded(rig, ts, thread_posts.Poster.for_session("s-other", "chat-mr-288"))
        rig.slack.human_says("and one from me")
        assert rig.connector._poll_thread() == 2
        authors = [record.source.author for record in rig.log.read_since(1)[0]]
        assert authors == ["lmer chat-mr-288", "tester"]
        assert [record.source.channel for record in rig.log.read_since(1)[0]] == ["slack"] * 2

    def test_it_is_not_mirrored_straight_back_into_the_thread(self, rig):
        """A live inbound record is never mirrored back to its own channel."""
        rig.connector.start()
        rig.wait_until(lambda: any(
            record.text.startswith("[replay notice]") for record in rig.log.read_since(0)[0]
        ))
        ts = rig.slack.app_says("[lmer session s-other] have a look")
        self._recorded(rig, ts, thread_posts.Poster.for_session("s-other", "chat-mr-288"))
        rig.wait_until(lambda: any(
            "have a look" in record.text for record in rig.log.read_since(0)[0]
        ))
        rig.agent("message", "after")
        rig.wait_until(lambda: any(text == "after" for _c, _t, text in rig.slack.posted))
        assert not any("have a look" in text for _c, _t, text in rig.slack.posted)

    def test_another_runs_post_can_answer_an_open_question(self, rig):
        """§6 binding applies to it exactly as to a human message."""
        rig.connector._poll_thread(replay=True)
        question = rig.agent("question", "branch?", options=["main", "prep-release"])
        ts = rig.slack.app_says("2")
        self._recorded(rig, ts, thread_posts.Poster.for_session("s-other", "chat-mr-288"))
        assert rig.connector._poll_thread() == 1
        answer = rig.log.read_since(question.seq)[0][0]
        assert (answer.kind, answer.text, answer.in_reply_to) == (
            "answer", "prep-release", question.id,
        )
        assert answer.source.author == "lmer chat-mr-288"

    def test_a_post_read_before_the_host_recorded_it_is_held_not_lost(self, rig):
        """The registry entry is written just after Slack accepts the post, so a
        poll can land in between. Dropping it there would be this issue again."""
        rig.connector._poll_thread(replay=True)
        ts = rig.slack.app_says("[lmer session s-other] have a look")
        assert rig.connector._poll_thread() == 0
        self._recorded(rig, ts, thread_posts.Poster.for_session("s-other", "chat-mr-288"))
        assert rig.connector._poll_thread() == 1
        assert rig.log.read_since(1)[0][0].source.author == "lmer chat-mr-288"

    def test_attach_counts_another_runs_post_without_naming_its_author(self, rig):
        ts = rig.slack.app_says("[lmer session s-other] read this before you start")
        self._recorded(rig, ts, thread_posts.Poster.for_session("s-other", "chat-mr-288"))
        assert rig.connector._poll_thread(replay=True) == 1
        (notice,) = rig.log.read_since(0)[0]
        assert notice.source.author is None
        assert "has 2 earlier messages" in notice.text
        assert "chat-mr-288" not in notice.text and "UBOT" not in notice.text

    def test_the_batch_is_never_an_answer_even_from_another_run(self, rig):
        """#426's rule survives the merge: the backlog predates the attach, so
        it answers nothing and leaves the first-free-text slot alone."""
        question = rig.agent("question", "what should I call it?")
        ts = rig.slack.app_says("morning")
        self._recorded(rig, ts, thread_posts.Poster.for_session("s-other", "chat-mr-288"))
        assert rig.connector._poll_thread(replay=True) == 1
        replayed = rig.log.read_since(question.seq)[0][-1]
        assert (replayed.kind, replayed.in_reply_to) == ("message", None)
        assert rig.log.question(question.id).open

    def test_a_message_from_another_slack_app_is_left_alone(self, rig):
        """An xoxb post always carries the bot's own user id, so a message
        without it is not ours to attribute — and holding it would spend a hold
        slot and the warning on a stranger (review of !300)."""
        rig.connector._poll_thread(replay=True)
        rig.slack.replies.append({
            "ts": "1700001900.000400", "bot_id": "B0OTHER", "app_id": "A0OTHER",
            "text": "a build finished",
        })
        assert rig.connector._poll_thread() == 0
        assert rig.connector._posters._held == {}

    def test_the_mirror_carries_the_session_stamp_for_live_attribution(self, rig):
        """The criterion's third stamping site. A run that attaches to this
        thread afterwards is replayed the history, and without this the whole of
        this run's output is unattributable and dropped (review of !300)."""
        rig.connector._poll_thread(replay=True)
        ts = rig.slack.post_message("C1", "1699999999.000000", "\U0001f4dd working on it")
        rig.connector._mirrored(ts)
        assert thread_posts.lookup("C1", ts) == thread_posts.Poster.for_session("sess-1")
        assert rig.connector._poll_thread() == 0, "and this connector still skips it"

    def test_attribution_does_not_depend_on_which_thread_the_link_named(self, rig):
        differently_keyed = connector.SlackConnector(
            replace(rig.config, thread_ts="1699999000.000000"),
            slack=rig.slack, bus_client=rig.bus,
        )
        differently_keyed._poll_thread(replay=True)
        ts = rig.slack.app_says("[lmer session s-other] have a look")
        self._recorded(rig, ts, thread_posts.Poster.for_session("s-other", "chat-mr-288"))
        assert differently_keyed._poll_thread() == 1
        assert rig.log.read_since(1)[0][-1].source.author == "lmer chat-mr-288"

    def test_the_hold_ends_and_says_so_once(self, rig, caplog):
        now = [0.0]
        held = connector.SlackConnector(
            rig.config, slack=rig.slack, bus_client=rig.bus, clock=lambda: now[0],
        )
        held._poll_thread(replay=True)
        rig.slack.app_says("nobody recorded this")
        rig.slack.app_says("nor this")
        assert held._poll_thread() == 0
        now[0] = thread_posts.GRACE_SECONDS + 1
        with caplog.at_level("WARNING"):
            assert held._poll_thread() == 0
        lines = [r for r in caplog.records if "unattributed_post" in r.getMessage()]
        assert len(lines) == 1
        assert "docs/COMMS.md" in lines[0].getMessage()

    def test_a_reconnect_counts_both_sides_and_still_filters_live_posts(self, rig):
        rig.connector._poll_thread(replay=True)
        rig.slack.post_message("C1", "1699999999.000000", "working on it")
        rig.slack.app_says("have a look")
        rig.connector.stop()
        reconnected = connector.SlackConnector(rig.config, slack=rig.slack, bus_client=rig.bus)
        rig.connector = reconnected
        assert reconnected._poll_thread(replay=True) == 1
        assert "has 3 earlier messages" in rig.log.read_since(0)[0][-1].text
        rig.slack.post_message("C1", "1699999999.000000", "own output")
        ts = rig.slack.app_says("new post")
        self._recorded(rig, ts, thread_posts.Poster.for_session("s-other", "chat-mr-288"))
        assert reconnected._poll_thread() == 1
        assert rig.log.read_since(0)[0][-1].source.author == "lmer chat-mr-288"


class TestConfig:
    def test_from_env_reads_the_supervisor_and_the_thread(self):
        config = connector.ConnectorConfig.from_env({
            "LMER_FASTAPI_PORT": "8701", "LMER_FASTAPI_TOKEN": "t",
            "LMER_SLACK_CHANNEL": "C1", "LMER_SLACK_THREAD_TS": "1.2",
            "SLACK_BOT_TOKEN": "xoxb", "LMER_SLACK_POLL_SECONDS": "7",
        })
        assert config.supervisor_url == "http://127.0.0.1:8701" and config.poll_seconds == 7.0
        assert "xoxb" not in repr(config) and "t" == config.supervisor_token

    def test_from_env_refuses_a_missing_thread_or_token(self):
        with pytest.raises(ValueError):
            connector.ConnectorConfig.from_env({"LMER_FASTAPI_PORT": "1", "LMER_FASTAPI_TOKEN": "t", "SLACK_BOT_TOKEN": "x"})
        with pytest.raises(ValueError):
            connector.ConnectorConfig.from_env({
                "LMER_FASTAPI_PORT": "1", "LMER_FASTAPI_TOKEN": "t",
                "LMER_SLACK_CHANNEL": "C", "LMER_SLACK_THREAD_TS": "1",
            })
        with pytest.raises(bus.NoChannel):
            connector.ConnectorConfig.from_env({"LMER_SLACK_CHANNEL": "C", "LMER_SLACK_THREAD_TS": "1", "SLACK_BOT_TOKEN": "x"})

    def test_the_target_handler_keeps_the_tokens_out_and_names_the_channel(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-host")
        handler = targets.SlackThreadTargets()
        handler.add("https://example.slack.com/archives/C1/p1699999999000000")
        env = handler.container_env()
        assert env["SLACK_BOT_TOKEN"] is None and env["SLACK_APP_TOKEN"] is None
        assert env["LMER_CHAT_CHANNEL"] == "slack" and env["LMER_SLACK_CHANNEL"] == "C1"
        assert targets.SlackThreadTargets.needs_control_plane is True
        assert handler.validate_environment() is None

    def test_the_handler_passes_its_endpoint_and_uploads_to_the_child_owner(self, monkeypatch, tmp_path):
        started = []

        class FakeOwner:
            def __init__(self, *args):
                started.append(args)

            def start(self):
                started.append("start")

            def stop(self):
                started.append("stop")

        monkeypatch.setattr(targets, "ConnectorProcess", FakeOwner)
        handler = targets.SlackThreadTargets()
        link = "https://example.slack.com/archives/C1/p1699999999000000"
        handler.add(link)
        handler.attach_control_plane("http://127.0.0.1:8701", "tok")
        handler.attach_uploads(str(tmp_path), "/home/developer/.lmer-uploads")
        handler.on_session_start()
        assert started[0] == ("slack", "C1", "1699999999.000000", link,
                              ("http://127.0.0.1:8701", "tok"),
                              (str(tmp_path), "/home/developer/.lmer-uploads"))
        assert started[1] == "start"
        handler.on_session_end()
        assert started[-1] == "stop"

    def test_without_a_control_plane_the_handler_warns_instead_of_crashing(self, monkeypatch, capsys):
        handler = targets.SlackThreadTargets()
        handler.add("https://example.slack.com/archives/C1/p1699999999000000")
        handler.on_session_start()
        assert "without a supervisor control plane" in capsys.readouterr().err
        assert handler.connector is None


# ---------------------------------------------------------------------------
# started before the supervisor exists
# ---------------------------------------------------------------------------


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class TestStartingBeforeTheSupervisor:
    """The host CLI starts a connector *before* it launches the container, so
    for the first seconds — minutes, on a cold image — nothing is listening on
    the supervisor's port. These run against real sockets, because the bug the
    class exists for is a `requests` ConnectionError classified as "the session
    is over": an in-process bus is always already up and cannot show it.
    """

    # The connect budget outlasts every wait below, so a loaded run measures
    # the behaviour each test names instead of racing the connector's give-up.
    def _connector(self, tmp_path, port, *, budget=outlast(JOIN), notify=None):
        log = comms.MessageLog("sess-late", str(tmp_path / "messages.jsonl"))
        slack = FakeSlack()
        config = connector.ConnectorConfig(
            supervisor_url=f"http://127.0.0.1:{port}", supervisor_token="tok",
            channel="C1", thread_ts="1699999999.000000", bot_token="xoxb-test",
            poll_seconds=0.05, connect_budget=budget, attach_ack=False,
        )
        return log, slack, connector.SlackConnector(config, slack=slack, notify=notify)

    def _serve(self, log, port):
        buf = supervisor.OutputBuffer(limit=4096)
        app = supervisor._build_fastapi_app(buf, lambda data: len(data), "tok", comms=log)
        return supervisor._start_fastapi_server(app, "127.0.0.1", port)

    def test_it_waits_for_the_port_and_then_carries_the_thread(self, tmp_path):
        port = _free_port()
        log, slack, conn = self._connector(tmp_path, port)
        conn.start()
        time.sleep(QUIET)  # nothing is listening; the connector must still be alive
        assert not slack.posted
        thread, shutdown = self._serve(log, port)
        try:
            log.append(kind="milestone", text="pushed", source=comms.Source("agent"))
            deadline = time.monotonic() + JOIN
            while time.monotonic() < deadline and not slack.posted:
                time.sleep(0.05)
            assert [text for _, _, text in slack.posted] == ["🏁 pushed"]
        finally:
            conn.stop()
            shutdown()
            thread.join(timeout=JOIN)

    def test_a_supervisor_that_answers_without_a_bus_is_a_definite_no(self, tmp_path):
        """Not every refusal is worth waiting on: a pre-#407 image answers, and
        waiting 15 minutes for a bus it will never grow is not the behaviour."""
        port = _free_port()
        log, slack, conn = self._connector(tmp_path, port, budget=outlast(JOIN))
        buf = supervisor.OutputBuffer(limit=4096)
        app = supervisor._build_fastapi_app(buf, lambda data: len(data), "tok")  # no comms
        thread, shutdown = supervisor._start_fastapi_server(app, "127.0.0.1", port)
        said: list = []
        conn._notify = said.append
        try:
            conn.start()
            deadline = time.monotonic() + JOIN
            while time.monotonic() < deadline and not said:
                time.sleep(0.05)
            assert said and "never came up" in said[0], "a definite no must not wait out the budget"
        finally:
            conn.stop()
            shutdown()
            thread.join(timeout=JOIN)

    def test_the_give_up_is_said_out_loud(self, tmp_path):
        port = _free_port()
        said: list = []
        log, slack, conn = self._connector(tmp_path, port, budget=CONNECT_EXPIRED, notify=said.append)
        conn.start()
        deadline = time.monotonic() + JOIN
        while time.monotonic() < deadline and not said:
            time.sleep(0.05)
        conn.stop()
        assert said and "never came up" in said[0] and "will not hear the session" in said[0]

    def test_stopping_during_the_wait_says_nothing(self, tmp_path):
        """A session that ends while the connector is still waiting is not a
        failure to report."""
        port = _free_port()
        said: list = []
        log, slack, conn = self._connector(tmp_path, port, notify=said.append)
        conn.start()
        time.sleep(QUIET)
        conn.stop()
        time.sleep(QUIET)
        assert said == []


@pytest.mark.parametrize("reply, expected", [("3", "3"), ("5", "5"), ("10", "10"), ("1", "3"), ("3.", "10")])
def test_integer_option_text_precedes_ordinal(reply, expected):
    assert connector.option_for(reply, ["3", "5", "10"]) == expected


def test_failed_inbound_post_keeps_the_free_answer_retryable(rig, monkeypatch):
    rig.connector._poll_thread(replay=True)
    question = rig.agent("question", "which branch?")
    rig.slack.human_says("prep-release")
    with monkeypatch.context() as patcher:
        def fail(*args, **kwargs):
            raise bus.BusError("temporary bus refusal")
        patcher.setattr(rig.connector.bus, "post", fail)
        with pytest.raises(bus.BusError, match="temporary"):
            rig.connector._poll_thread()
    assert rig.connector._poll_thread() == 1
    answer = rig.log.read_since(question.seq)[0][-1]
    assert (answer.kind, answer.text, answer.in_reply_to) == ("answer", "prep-release", question.id)


@pytest.mark.parametrize("renderer", ["slack", "matrix"])
@pytest.mark.parametrize("fidelity", ["reconstructed", "confirmed"])
def test_reconstructed_terminal_text_is_marked_in_both_threads(renderer, fidelity):
    from matrix_chat.connector import render_outbound as matrix_render
    render = connector.render_outbound if renderer == "slack" else matrix_render
    record = {"kind": "message", "text": "words", "source": {"channel": "terminal", "author": "alice"},
              "meta": {"fidelity": fidelity}}
    assert "terminal · alice · typed" in render(record)
    record["meta"] = {}
    assert "typed" not in render(record)


@pytest.mark.parametrize("transport", ["slack", "matrix"])
@pytest.mark.parametrize("alternate_spellings", [False, True])
def test_each_thread_starts_and_stops_its_own_connector(tmp_path, monkeypatch, transport, alternate_spellings):
    from matrix_chat import connector as matrix_connector

    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-bot")
    monkeypatch.setenv("LMER_MATRIX_HOMESERVER", "https://matrix.example.org")
    monkeypatch.setenv("LMER_MATRIX_USER_ID", "@bot:example.org")
    monkeypatch.setenv("LMER_MATRIX_ACCESS_TOKEN", "test-access")
    links = (
        ["https://team.slack.com/archives/C1/p1700000000000001", "https://team.slack.com/archives/C2/p1700000000000002"]
        if transport == "slack" else
        ["https://matrix.to/#/!room:example.org/$one", "https://matrix.to/#/!room:example.org/$two"]
    )
    if alternate_spellings:
        suffix = "?cid=C1" if transport == "slack" else "?via=example.org"
        links = [links[0], links[0] + suffix, links[1], links[1] + suffix, *links]
        links.append(links[0] + "/")
        if transport == "matrix":
            links.append("https://element.example.org/#/room/%21room%3Aexample.org/%24one")
    started, stopped = [], []
    monkeypatch.setattr(targets.ConnectorProcess, "start", lambda instance: started.append(instance))
    monkeypatch.setattr(targets.ConnectorProcess, "stop", lambda instance: stopped.append(instance))
    _, handlers = targets.partition_targets(["fleet", *links])
    handler = handlers[0]
    handler.attach_control_plane("http://localhost:8765", "test-control")
    handler.attach_uploads(str(tmp_path), "/home/developer/.lmer-uploads")
    handler.on_session_start()
    assert len(started) == 2
    assert len({child.key for child in started}) == 2
    assert all(child.endpoint == ("http://localhost:8765", "test-control") for child in started)
    assert all(child.uploads == (str(tmp_path), "/home/developer/.lmer-uploads") for child in started)
    assert not targets.special_target_env(handlers)["SLACK_BOT_TOKEN"]
    assert not targets.special_target_env(handlers)["LMER_MATRIX_ACCESS_TOKEN"]
    handler.on_session_end()
    assert set(stopped) == set(started)


@pytest.mark.parametrize("channel, thread_ts", [("C2", "1699999999.000000"), ("C1", "1699999999.000001")])
def test_same_timestamp_in_distinct_slack_threads_reaches_bus_and_pty(rig, channel, thread_ts):
    other = connector.SlackConnector(
        replace(rig.config, channel=channel, thread_ts=thread_ts),
        slack=FakeSlack(), bus_client=rig.connector.bus,
    )
    first = {"ts": "1700000000.000001", "user": "U1", "text": "first conversation"}
    second = {**first, "text": "second conversation"}
    assert rig.connector._post_inbound(first)["accepted_as"] == "new"
    assert other._post_inbound(second)["accepted_as"] == "new"
    assert rig.connector._post_inbound(first)["accepted_as"] == "duplicate"
    assert other._post_inbound(second)["accepted_as"] == "duplicate"
    records = rig.log.read_since(0)[0]
    assert [record.text for record in records] == [first["text"], second["text"]]
    assert len({record.source.ref for record in records}) == 2
    assert b"".join(rig.sink).count(b"first conversation") == 1
    assert b"".join(rig.sink).count(b"second conversation") == 1


def test_assistant_reply_reaches_slack_and_human_reply_reaches_its_pty(rig):
    rig.log.agent_channel = comms.CHANNEL_UBER
    rig.connector.start()
    try:
        rig.agent("message", "hello from the assistant")
        rig.wait_until(lambda: any("[uber lmer] hello from the assistant" in text for _, _, text in rig.slack.posted))
        rig.slack.human_says("hello back")
        rig.wait_until(lambda: b"hello back" in b"".join(rig.sink))
        assert b"[slack \xc2\xb7 tester] hello back" in b"".join(rig.sink)
        assert b"hello from the assistant" not in b"".join(rig.sink)
        assert [r.source.channel for r in rig.log.read_since(0)[0] if r.text == "hello from the assistant"] == ["uber-lmer"]
    finally:
        rig.connector.stop()


@pytest.mark.parametrize("launch, expected", [
    ({"preset": "astra", "model": "gpt-test"}, "Preset: astra; model: gpt-test."),
    ({"preset": None, "model": None}, "Preset: none; model: harness default."),
    ({"preset": "a<&b", "model": "<@everyone>\nmodel"},
     "Preset: a&lt;&amp;b; model: &lt;@everyone&gt; model."),
    (None, ""),
])
def test_attachment_notice_names_the_supervisors_launch_settings(tmp_path, launch, expected):
    rig = Rig(tmp_path, launch_settings=launch)
    rig.connector.config = replace(rig.config, attach_ack=True)
    rig.connector._seed_cursor()
    rig.connector._announce_attached()
    rig.connector._announce_attached()
    text = connector.ATTACH_ACK + (" " + expected if expected else "")
    assert [posted[2] for posted in rig.slack.posted] == [text]
    assert rig.slack.replies[-1]["blocks"][0]["block_id"].startswith(connector.CONNECTION_NOTICE_BLOCK_PREFIX)
    rig.log.close()


def test_attachment_metadata_does_not_override_the_ack_off_switch(tmp_path):
    rig = Rig(tmp_path, launch_settings={"preset": "astra", "model": "gpt-test"})
    rig.connector._seed_cursor()
    rig.connector._announce_attached()
    assert rig.slack.posted == []
    rig.log.close()


# ---------------------------------------------------------------------------
# credential redaction at the outbound boundary (issue #539)
# ---------------------------------------------------------------------------

# Fabricated: a GitHub-shaped token and a value that only the environment knows.
_SHAPED = "ghp_" + "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0Uv1Wx2Yz3"
_ENV_VALUE = "fabricated-env-value-0123456789"


class TestOutboundRedaction:
    @pytest.fixture(autouse=True)
    def _env_secret(self, monkeypatch):
        monkeypatch.setenv("LMER_FABRICATED_API_TOKEN", _ENV_VALUE)

    def _record(self, kind, text, **extra):
        return {"id": f"sess-1/{kind}", "kind": kind, "text": text,
                "source": {"channel": "agent"}, "meta": {}, **extra}

    @pytest.mark.parametrize("secret", [_SHAPED, _ENV_VALUE], ids=["shape", "env-value"])
    @pytest.mark.parametrize("kind", ["message", "milestone", "command", "close"])
    def test_every_kind_reaches_slack_redacted(self, rig, kind, secret):
        rig.connector._send_record(self._record(kind, f"using {secret} now"))
        (_, _, posted), = rig.slack.posted
        assert secret not in posted
        assert "***REDACTED***" in posted

    @pytest.mark.parametrize("secret", [_SHAPED, _ENV_VALUE], ids=["shape", "env-value"])
    def test_question_text_and_options_are_redacted(self, rig, secret):
        rig.connector._send_record(self._record(
            "question", f"use {secret}?", options=[f"yes {secret}", "no"],
        ))
        (_, _, posted), = rig.slack.posted
        assert secret not in posted
        assert posted.startswith("❓ use ***REDACTED***?\n1. yes ***REDACTED***\n2. no\n")

    def test_attachment_names_and_the_automatic_terminal_mirror_are_redacted(self, rig):
        record = self._record("message", "typed", attachments=[{"name": f"{_SHAPED}.txt"}])
        record["source"] = {"channel": "terminal", "author": _ENV_VALUE}
        record["meta"] = {"fidelity": "confirmed"}
        rig.connector._send_record(record)
        (_, _, posted), (_, _, note) = rig.slack.posted
        assert "withheld" in note, "the file is not in the store, so it is withheld (#459)"
        for text in (posted, note):
            assert _SHAPED not in text and _ENV_VALUE not in text

    def test_the_quoted_question_in_an_answer_line_is_redacted(self, rig):
        question = self._record("question", f"use {_SHAPED}?", id="q1")
        answer = self._record("answer", "yes", in_reply_to="q1")
        answer["source"] = {"channel": "matrix", "author": "@a:x"}
        assert _SHAPED not in connector.render_outbound(answer, {"q1": question})

    def test_the_connection_notice_is_redacted(self, rig):
        rig.connector._send_notice(f"launched with {_ENV_VALUE}")
        assert _ENV_VALUE not in rig.slack.posted[0][2]

    def test_a_credential_cut_by_the_clip_is_still_redacted(self, rig):
        # The token straddles the limit: clipped first, its surviving prefix
        # would no longer look like a token.
        lead = "x " * ((connector.MAX_SLACK_CHARS - 12) // 2)
        rig.connector._send_record(self._record("message", lead + _SHAPED))
        posted = rig.slack.posted[0][2]
        assert "ghp_" not in posted
        assert len(posted) <= connector.MAX_SLACK_CHARS

    def test_a_credential_cut_by_the_answer_headline_is_still_redacted(self):
        # _head keeps 80 characters of the question it quotes.
        text = "a" * 70 + _SHAPED
        question = self._record("question", text, id="q1")
        answer = self._record("answer", "yes", in_reply_to="q1")
        answer["source"] = {"channel": "matrix", "author": "@a:x"}
        assert "ghp_" not in connector.render_outbound(answer, {"q1": question})

    def test_the_bus_record_is_not_rewritten_and_the_answer_still_routes(self, rig):
        rig.connector._poll_thread(replay=True)
        question = rig.agent("question", "which?", options=[f"use {_SHAPED}", "no"])
        record = question.to_dict()
        rig.connector._send_record(record)
        assert _SHAPED not in rig.slack.posted[-1][2]
        assert record["options"][0] == f"use {_SHAPED}"
        rig.slack.human_says("1")
        assert rig.connector._poll_thread() == 1
        answer = rig.log.read_since(question.seq)[0][0]
        assert (answer.kind, answer.text, answer.in_reply_to) == ("answer", f"use {_SHAPED}", question.id)

    def test_ordinary_text_is_untouched(self, rig):
        rig.connector._send_record(self._record("message", "branch main is green"))
        assert rig.slack.posted[0][2] == "branch main is green"

    def test_matrix_headline_of_a_quoted_question_is_redacted(self):
        # Matrix clips its own copy too, so the leak this pins is the 80-character
        # headline, which the shared renderer used to cut before Matrix scrubbed.
        from matrix_chat.connector import render_outbound as matrix_render

        question = self._record("question", "a" * 70 + _SHAPED, id="q1")
        answer = self._record("answer", "yes", in_reply_to="q1")
        answer["source"] = {"channel": "slack", "author": "tester"}
        assert "ghp_" not in matrix_render(answer, {"q1": question})

    def test_a_launch_label_is_scrubbed_before_it_is_clipped_and_escaped(self, monkeypatch):
        # The escape changes this value itself, so only a scrub of the raw label finds it.
        special = "fabricated<&>label-value-0123456789"
        monkeypatch.setenv("LMER_FABRICATED_ESCAPE_TOKEN", special)
        for markup in (True, False):
            clipped = connector.attachment_notice(
                {"preset": "p" * 150 + " " + _SHAPED}, slack_markup=markup,
            )
            assert "ghp_" not in clipped
            text = connector.attachment_notice({"model": special}, slack_markup=markup)
            assert special not in text and html.escape(special, quote=False) not in text

    def test_the_automatic_loop_posts_only_redacted_json_through_the_real_client(self, rig):
        """Bus record → ``_outbound_once`` → ``SlackClient`` → the request JSON."""
        from slack_chat.client import SlackClient

        sent = []

        class _Response:
            headers: dict = {}

            def raise_for_status(self):
                pass

            def json(self):
                return {"ok": True, "ts": f"1700000500.{len(sent):06d}"}

        client = SlackClient("xoxb-test")
        client.session.request = lambda method, url, **kw: (sent.append((url, kw.get("json"))), _Response())[1]
        rig.connector._slack = client
        rig.connector._ensure_ready()
        rig.agent("question", f"use {_SHAPED}?", options=[f"yes {_ENV_VALUE}", "no"])
        rig.connector._outbound_once()
        rig.connector._notice(f"launched with {_SHAPED} {_ENV_VALUE}", "attached")
        assert len(sent) == 2
        for url, payload in sent:
            assert url.endswith("/chat.postMessage")
            assert _SHAPED not in repr(payload) and _ENV_VALUE not in repr(payload)
        assert sent[0][1]["text"].startswith("❓ use ***REDACTED***?\n1. yes ***REDACTED***\n2. no\n")
        assert "***REDACTED***" in sent[1][1]["blocks"][0]["text"]["text"]
