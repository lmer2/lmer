"""`Source.author_id`: the channel's stable id for the human who wrote a record.

``author`` is display text (a Slack display name, a host-stamped run label), so
it cannot say which person a post came from. The Slack and Matrix connectors
put the sender's own id on a human's record — the Slack user id, the MXID —
and never on a post the shared bot account made, whether another run's (a
host-stamped poster) or the bot's own. Older records without the field parse
unchanged.
"""
from __future__ import annotations

import pytest

from lmer_cli import comms, thread_posts
from matrix_chat import connector as matrix_connector
from slack_chat import connector as slack_connector
from slack_chat import registry
from tests.conftest import drain_connectors, strip_lmer_env
from tests.test_matrix_chat import BOT, Rig as MatrixRig
from tests.test_slack_connector import Rig as SlackRig
from tests.timing_budgets import DRAIN


@pytest.fixture(autouse=True)
def isolated_connectors(tmp_path, monkeypatch):
    strip_lmer_env(monkeypatch)
    monkeypatch.setattr(thread_posts, "REGISTRY_DIR", str(tmp_path / "thread-posts"))
    monkeypatch.setattr(registry, "REGISTRY_DIR", str(tmp_path / "thread-owners"))
    instances = []
    for cls in (slack_connector.SlackConnector, matrix_connector.MatrixConnector):
        original_init = cls.__init__

        def tracked_init(self, *args, _original=original_init, **kwargs):
            _original(self, *args, **kwargs)
            instances.append(self)

        monkeypatch.setattr(cls, "__init__", tracked_init)
    yield
    drain_connectors(instances, DRAIN)


@pytest.fixture
def slack(tmp_path):
    rig = SlackRig(tmp_path)
    yield rig
    rig.connector.stop()
    rig.bus._session._http.close()
    rig.log.close()


@pytest.fixture
def matrix(tmp_path):
    rig = MatrixRig(tmp_path)
    yield rig
    rig.connector.stop()
    rig.bus._session._http.close()
    rig.log.close()


def _by_text(rig, text):
    records = [record for record in rig.log.read_since(0)[0] if record.text == text]
    assert len(records) == 1, f"expected one record with text {text!r}"
    return records[0]


# ---------------------------------------------------------------------------
# the field
# ---------------------------------------------------------------------------


class TestSourceField:
    def test_round_trip_with_author_id(self):
        source = comms.Source("slack", author="tester", ref="C1:1.0:2.0", author_id="U1")
        raw = source.to_dict()
        assert raw == {"channel": "slack", "author": "tester", "ref": "C1:1.0:2.0", "author_id": "U1"}
        assert comms.Source.from_dict(raw) == source

    def test_round_trip_without_author_id_keeps_the_old_shape(self):
        source = comms.Source("matrix", author="@tester:example.org", ref="$e1")
        raw = source.to_dict()
        assert raw == {"channel": "matrix", "author": "@tester:example.org", "ref": "$e1"}
        assert comms.Source.from_dict(raw) == source
        assert comms.Source.from_dict(raw).author_id is None

    def test_an_older_record_on_disk_reads_back_unchanged(self, tmp_path):
        path = str(tmp_path / "messages.jsonl")
        log = comms.MessageLog("sess-1", path)
        try:
            log.append(kind="message", text="old", source=comms.Source("slack", author="tester"))
            log.append(kind="message", text="new",
                       source=comms.Source("slack", author="tester", author_id="U1"))
        finally:
            log.close()
        reopened = comms.MessageLog("sess-1", path)
        try:
            old, new = reopened.read_since(0)[0]
        finally:
            reopened.close()
        assert old.source == comms.Source("slack", author="tester")
        assert "author_id" not in old.to_dict()["source"]
        assert new.source.author_id == "U1"
        assert new.to_dict()["source"]["author_id"] == "U1"

    def test_blank_author_id_is_absent(self):
        assert comms.Source.from_dict({"channel": "slack", "author_id": "  "}).author_id is None

    @pytest.mark.parametrize("value, message", [
        (42, "source.author_id must be text"),
        ("U" * (comms.MAX_AUTHOR_CHARS + 1), "source.author_id"),
    ])
    def test_a_bad_author_id_is_refused(self, value, message):
        with pytest.raises(comms.CommsError, match=message):
            comms.Source.from_dict({"channel": "slack", "author_id": value})


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


class TestSlackInbound:
    def test_a_humans_post_carries_the_slack_user_id(self, slack):
        slack.connector._poll_thread(replay=True)
        slack.slack.human_says("from a person")
        assert slack.connector._poll_thread() == 1
        record = _by_text(slack, "from a person")
        assert record.source.author == "tester"
        assert record.source.author_id == "U1"

    def test_another_runs_post_carries_no_author_id(self, slack):
        slack.connector._poll_thread(replay=True)
        ts = slack.slack.app_says("from another run")
        assert thread_posts.record("C1", ts, thread_posts.Poster.for_session("s-other", "chat-mr-288"))
        assert slack.connector._poll_thread() == 1
        record = _by_text(slack, "from another run")
        assert record.source.author == "lmer chat-mr-288"
        assert record.source.author_id is None

    @pytest.mark.parametrize("message", [
        {"ts": "1700002000.000100", "user": "UBOT", "text": "from the bot itself"},
        {"ts": "1700002000.000200", "user": "U2", "bot_id": "B0OTHER", "text": "from the bot itself"},
    ], ids=["own-bot-user", "other-app-bot"])
    def test_a_bot_post_carries_no_author_id(self, slack, message):
        slack.connector._poll_thread(replay=True)
        assert slack.connector._post_inbound(message)["accepted_as"] == "new"
        assert _by_text(slack, "from the bot itself").source.author_id is None


# ---------------------------------------------------------------------------
# Matrix
# ---------------------------------------------------------------------------


class TestMatrixInbound:
    def test_a_humans_post_carries_the_sender_mxid(self, matrix):
        matrix.connector.replay()
        matrix.matrix.human_says("from a person", sender="@person:example.org")
        assert matrix.connector.sync_once() == 1
        record = _by_text(matrix, "from a person")
        assert record.source.author == "@person:example.org"
        assert record.source.author_id == "@person:example.org"

    def test_another_runs_post_carries_no_author_id(self, matrix):
        matrix.connector.replay()
        matrix.matrix.app_says("from another run",
                               poster=thread_posts.Poster.for_session("s-other", "chat-mr-288"))
        assert matrix.connector.sync_once() == 1
        record = _by_text(matrix, "from another run")
        assert record.source.author == "lmer chat-mr-288"
        assert record.source.author_id is None

    def test_the_bots_own_post_carries_no_author_id(self, matrix):
        matrix.connector.replay()
        event = matrix.matrix.human_says("from the bot itself", sender=BOT)
        assert matrix.connector._post_inbound(event) is True
        record = _by_text(matrix, "from the bot itself")
        assert record.source.author == BOT
        assert record.source.author_id is None
