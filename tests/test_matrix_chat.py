"""Matrix as a chat channel (issue #407, T6): the mirror of the Slack connector.

A fake homeserver stands in for the client-server API; the bus is real (the
supervisor's FastAPI routes over a real ``MessageLog``). Pinned: permalink
parsing, the reply-fallback rule, thread-root resolution, mention detection
and the allowlist, both connector directions, the target handler, and the
refusal of an encrypted room.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ask_channel import bus
from lmer_cli import comms, supervisor, targets, thread_posts
from matrix_chat import connector, listener, permalink
from matrix_chat.client import MatrixError
from slack_chat import registry
from tests.conftest import drain_connectors, strip_lmer_env
from tests.timing_budgets import DRAIN, JOIN
from tests.test_lmer_signal_cli import _Session


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch, tmp_path):
    from slack_chat import registry

    strip_lmer_env(monkeypatch)
    monkeypatch.setattr(registry, "REGISTRY_DIR", str(tmp_path / "thread-owners"))
    monkeypatch.setattr(thread_posts, "REGISTRY_DIR", str(tmp_path / "thread-posts"))
    instances = []
    original_init = connector.MatrixConnector.__init__

    def tracked_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._bus_poll_seconds = 0.05
        instances.append(self)

    monkeypatch.setattr(connector.MatrixConnector, "__init__", tracked_init)
    yield
    drain_connectors(instances, DRAIN)


ROOM = "!room:example.org"
ROOT = "$root_event_id"
BOT = "@lmer:example.org"


# ---------------------------------------------------------------------------
# permalinks and pure rules
# ---------------------------------------------------------------------------


class TestPermalink:
    @pytest.mark.parametrize("url, room, event", [
        ("https://matrix.to/#/!abc:example.org/$evt?via=example.org", "!abc:example.org", "$evt"),
        ("https://matrix.to/#/%21abc%3Aexample.org/%24evt", "!abc:example.org", "$evt"),
        ("https://matrix.to/#/#alias:example.org/$evt", "#alias:example.org", "$evt"),
        ("https://app.element.io/#/room/!abc:example.org/$evt", "!abc:example.org", "$evt"),
        ("https://chat.example.org/#/room/%23alias%3Aexample.org/%24e_1", "#alias:example.org", "$e_1"),
    ])
    def test_both_shapes_parse(self, url, room, event):
        parsed = permalink.parse_matrix_permalink(url)
        assert (parsed.room, parsed.event_id) == (room, event)
        assert permalink.is_matrix_thread_url(url)

    @pytest.mark.parametrize("url", [
        "https://matrix.to/#/!abc:example.org",               # a room, no event
        "https://example.slack.com/archives/C1/p1699999999000000",
        "https://gitlab.example.com/agents/global/-/issues/407",
        "not a url", "", None,
    ])
    def test_everything_else_is_not_a_matrix_thread(self, url):
        assert not permalink.is_matrix_thread_url(url)

    @pytest.mark.parametrize("value, is_room", [
        ("!abc:example.org", True),
        ("#alias:example.org", True),
        ("!abc:example.org:8448", True),
        ("#general", False),
        ("C0123ABCDEF", False),
        ("!abc", False),
        ("", False),
        (None, False),
    ])
    def test_a_room_is_read_the_same_named_alone_as_inside_a_link(self, value, is_room):
        """What `lmer-signal thread open` may be given (#429), by the reading a
        permalink already uses — a server name with a port included."""
        assert permalink.is_matrix_room(value) is is_room

    def test_the_canonical_link_round_trips(self):
        link = permalink.matrix_to_link(ROOM, ROOT)
        parsed = permalink.parse_matrix_permalink(link)
        assert (parsed.room, parsed.event_id) == (ROOM, ROOT)


class TestRules:
    def test_a_real_reply_fallback_is_stripped(self):
        body = "> <@a:x> earlier words\n> more of them\n\nuse that"
        assert connector.strip_reply_fallback(body) == "use that"

    def test_a_body_that_merely_starts_with_a_quote_survives(self):
        body = ">= 3.12 is required\nuse that"
        assert connector.strip_reply_fallback(body) == body

    def test_thread_root_is_the_relation_or_the_event_itself(self):
        reply = {"event_id": "$reply", "content": {"m.relates_to": {"rel_type": "m.thread", "event_id": ROOT}}}
        assert connector.resolve_thread_root(reply) == ROOT
        assert connector.resolve_thread_root({"event_id": "$top", "content": {"body": "hi"}}) == "$top"

    @pytest.mark.parametrize("content, expected", [
        ({"body": "hey @lmer help", "m.mentions": {}}, True),
        ({"body": "hey", "m.mentions": {"user_ids": [BOT]}}, True),
        ({"body": f"{BOT}: please", }, True),
        ({"body": "nothing for the bot"}, False),
    ])
    def test_mentions(self, content, expected):
        assert listener.is_mention({"content": content}, BOT) is expected

    def test_slack_style_rendering_is_reused_and_scrubbed(self):
        text = connector.render_outbound({"kind": "message", "text": "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789", "source": {"channel": "agent"}, "meta": {}})
        assert text and "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in text
        assert connector.render_outbound({"kind": "message", "text": "x", "source": {"channel": "matrix"}, "meta": {}}) is None


# ---------------------------------------------------------------------------
# a fake homeserver
# ---------------------------------------------------------------------------


class FakeMatrix:
    def __init__(self, *, encrypted=False):
        self.encrypted = encrypted
        self.sent: list = []
        self.stamps: list = []
        self.thread: list = []
        self.pending: list = []
        self.uploads: list = []
        self.next_batch = 0
        self.media = {}
        self.counter = 0
        self.reactions = []
        self.root = {"event_id": ROOT, "sender": "@tester:example.org", "type": "m.room.message",
                     "content": {"msgtype": "m.text", "body": "@lmer help me"}, "origin_server_ts": 1}

    def resolve_room(self, room):
        return ROOM

    def send_reaction(self, room, event, key, *, transaction):
        reaction = (room, event, key, transaction)
        if reaction not in self.reactions:
            self.reactions.append(reaction)
        return "$reaction"

    def join(self, room):
        return ROOM

    def room_is_encrypted(self, room_id):
        return self.encrypted

    def get_event(self, room_id, event_id):
        return self.root

    def thread_events(self, room_id, root, limit=100):
        return list(self.thread)

    def sync(self, *, since=None, filter_body=None, timeout_ms=0):
        self.next_batch += 1
        events, self.pending = self.pending, []
        return {"next_batch": f"s{self.next_batch}", "rooms": {"join": {ROOM: {"timeline": {"events": events}}}}}

    def send_thread_text(self, room_id, root, text, *, last_event_id=None, msgtype="m.text",
                         extra=None):
        event_id = f"$bot{len(self.sent)}"
        self.sent.append((room_id, root, text, msgtype))
        self.stamps.append(extra or {})
        return event_id

    def send_thread_file(self, room_id, root, *, mxc, name, media_type, size,
                         last_event_id=None, extra=None):
        event_id = f"$file{len(self.sent)}"
        self.sent.append((room_id, root, f"FILE {name} {mxc}", "m.file"))
        self.stamps.append(extra or {})
        return event_id

    def upload(self, data, *, content_type, filename):
        self.uploads.append((filename, content_type, data))
        return f"mxc://example.org/{len(self.uploads)}"

    def download(self, mxc, *, max_bytes=None):
        """The client's own shape since the read path bounded it: past
        *max_bytes* the body is not returned at all."""
        data, media_type = self.media[mxc]
        if max_bytes is not None and len(data) > max_bytes:
            return None, media_type
        return data, media_type

    def app_says(self, text, *, poster=None, live=True):
        """An event by the bot account every lmer session shares — what another
        run's `post --thread` and the platform's own post verb leave behind."""
        return self.human_says(
            text, sender=BOT, live=live, **thread_posts.matrix_content(poster),
        )

    def human_says(self, text, *, sender="@tester:example.org", live=True, **content):
        self.counter += 1
        event = {
            "event_id": f"$h{self.counter}", "sender": sender, "type": "m.room.message",
            "origin_server_ts": 10 + self.counter,
            "content": {"msgtype": "m.text", "body": text, "m.relates_to": {"rel_type": "m.thread", "event_id": ROOT}, **content},
        }
        (self.pending if live else self.thread).append(event)
        return event


class Rig:
    def __init__(self, tmp_path, *, uploads=None, encrypted=False, authenticated_media=False,
                 launch_settings=None):
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
        client = bus.BusClient(bus.Endpoint(url="http://testserver", token="tok"), session=_Session(TestClient(app)))
        self.bus = client
        self.matrix = FakeMatrix(encrypted=encrypted)
        self.config = connector.ConnectorConfig(
            supervisor_url="http://testserver", supervisor_token="tok",
            homeserver="https://hs.example.org", user_id=BOT, access_token="syt_secret",
            room=ROOM, thread_event=ROOT, authenticated_media=authenticated_media,
            uploads_dir=uploads, container_uploads_dir="/home/developer/.lmer-uploads",
            # As in the Slack rig: the attach ack has its own tests, and it would
            # otherwise head every list of posted events here.
            attach_ack=False,
        )
        self.connector = connector.MatrixConnector(self.config, matrix=self.matrix, bus_client=client)
        self.connector._bus_poll_seconds = 0.05

    def agent(self, kind, text, **kw):
        return self.log.append(kind=kind, text=text, source=comms.Source("agent"), **kw)[0]

    def wait_until(self, predicate, timeout=JOIN):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert predicate(), "condition not reached in time"


@pytest.fixture
def rig(tmp_path):
    rig = Rig(tmp_path)
    yield rig
    rig.connector.stop()
    rig.bus._session._http.close()
    rig.log.close()


# ---------------------------------------------------------------------------
# connector
# ---------------------------------------------------------------------------


class TestConnector:
    def test_peer_ul_records_never_post_but_worker_messages_still_do(self, rig):
        record = {"id": "sess-1/peer", "kind": "message", "text": "Private native coordination",
                  "source": {"channel": "peer-ul", "author": "Example Alpha"}, "meta": {}}
        rig.connector._post_outbound(record, {})
        assert rig.matrix.sent == []
        rig.connector._post_outbound({**record, "id": "sess-1/worker", "source": {"channel": "uber-lmer"}}, {})
        assert len(rig.matrix.sent) == 1

    @pytest.mark.parametrize("with_replies", [False, True])
    def test_default_attach_and_restart_exclude_lifecycle_notices(self, rig, monkeypatch, with_replies):
        from dataclasses import replace

        newest = ROOT
        if with_replies:
            rig.matrix.human_says(connector.ATTACH_ACK, live=False)
            newest = rig.matrix.human_says("a previous session reply", sender=BOT, live=False)["event_id"]
        post = rig.matrix.send_thread_text

        def store_post(room, root, text, **kwargs):
            event_id = post(room, root, text, **kwargs)
            rig.matrix.thread.append({
                "event_id": event_id, "sender": BOT, "type": "m.room.message",
                "origin_server_ts": 1000 + len(rig.matrix.sent),
                "content": {"msgtype": kwargs.get("msgtype", "m.text"), "body": text,
                            "m.relates_to": {"rel_type": "m.thread", "event_id": ROOT},
                            **(kwargs.get("extra") or {})},
            })
            return event_id

        monkeypatch.setattr(rig.matrix, "send_thread_text", store_post)
        for _ in range(2):
            current = connector.MatrixConnector(replace(rig.config, attach_ack=True),
                                                matrix=rig.matrix, bus_client=rig.bus)
            current.start()
            try:
                rig.wait_until(lambda: current._next_batch is not None)
                (notice,) = rig.log.read_since(0)[0]
                assert f"has {3 if with_replies else 1} earlier message" in notice.text
                assert notice.source.ref == newest
            finally:
                current.stop()

    def test_an_encrypted_room_is_refused_by_name(self, tmp_path):
        rig = Rig(tmp_path, encrypted=True)
        with pytest.raises(MatrixError, match="end-to-end encrypted"):
            rig.connector.connect()

    def test_attach_counts_both_sides_without_replaying_them(self, rig):
        rig.matrix.human_says("earlier reply", live=False)
        newest = rig.matrix.human_says("bot echo", sender=BOT, live=False)
        assert rig.connector.replay() == 1
        (record,) = rig.log.read_since(0)[0]
        assert (record.kind, record.in_reply_to) == ("message", None)
        assert record.source == comms.Source("matrix", ref=newest["event_id"])
        assert len(record.text.splitlines()) == 1
        assert "has 3 earlier messages" in record.text
        assert "earlier reply" not in record.text and "bot echo" not in record.text
        assert b"[matrix] [replay notice]" in b"".join(rig.sink)
        assert rig.connector.sync_once() == 0

    def test_live_replies_answer_the_oldest_open_question(self, rig):
        rig.connector.replay()
        question = rig.agent("question", "branch?", options=["main", "prep-release"])
        rig.matrix.human_says("2")
        assert rig.connector.sync_once() == 1
        answer = rig.log.read_since(question.seq)[0][0]
        assert (answer.kind, answer.text, answer.in_reply_to) == ("answer", "prep-release", question.id)
        rig.matrix.human_says("> <@lmer:example.org> branch?\n\nthanks!")
        rig.connector.sync_once()
        assert rig.log.read_since(0)[0][-1].text == "thanks!"

    def test_a_reply_to_the_question_event_binds_whatever_it_says(self, rig):
        """The relation matrix has and Slack does not: a human replying *to* the
        question has said this is the answer, so it binds as typed (operator's
        ruling on decision 12)."""
        rig.connector.replay()
        question = rig.agent("question", "branch?", options=["main", "prep-release"])
        rig.connector._post_outbound(
            {"kind": "question", "id": question.id, "text": "branch?", "seq": question.seq,
             "options": ["main", "prep-release"], "source": {"channel": "agent"}, "meta": {}}, {},
        )
        posted = rig.connector._question_events[question.id]
        rig.matrix.human_says(
            "the release one", **{"m.relates_to": {
                "rel_type": "m.thread", "event_id": ROOT,
                "m.in_reply_to": {"event_id": posted}, "is_falling_back": False,
            }},
        )
        assert rig.connector.sync_once() == 1
        answer = rig.log.read_since(question.seq)[0][0]
        assert (answer.kind, answer.text) == ("answer", "the release one")

    def test_conversation_beside_an_open_question_is_not_its_answer(self, rig):
        rig.connector.replay()
        question = rig.agent("question", "branch?", options=["main", "prep-release"])
        rig.matrix.human_says("hold on, let me look")
        assert rig.connector.sync_once() == 1
        last = rig.log.read_since(question.seq)[0][-1]
        assert last.kind == "message" and last.text == "hold on, let me look"
        assert rig.log.question(str(question.seq)).open

    def test_events_outside_the_thread_and_the_bots_own_are_ignored(self, rig):
        rig.connector.replay()
        rig.matrix.pending.append({"event_id": "$other", "sender": "@tester:example.org", "type": "m.room.message",
                                   "content": {"msgtype": "m.text", "body": "in another thread",
                                               "m.relates_to": {"rel_type": "m.thread", "event_id": "$elsewhere"}}})
        rig.matrix.human_says("from the bot", sender=BOT)
        rig.matrix.pending.append({"event_id": "$enc", "sender": "@tester:example.org", "type": "m.room.encrypted", "content": {}})
        assert rig.connector.sync_once() == 0

    def test_agent_records_reach_the_thread_and_are_acked(self, rig):
        rig.connector.start()
        rig.agent("note", "working")
        rig.agent("question", "branch?", options=["main"])
        rig.wait_until(lambda: len([s for s in rig.matrix.sent if s[3] == "m.text"]) >= 1)
        texts = [s[2] for s in rig.matrix.sent if s[3] == "m.text"]
        assert texts == ["❓ branch?\n1. main\n_reply in this thread with a number or your own words — the first answer from any channel wins_"]
        assert all(s[:2] == (ROOM, ROOT) for s in rig.matrix.sent)
        rig.wait_until(lambda: rig.log.subscribers().get(rig.connector.subscriber) == rig.log.seq)

    def test_matrix_records_are_not_posted_back(self, rig):
        rig.connector.start()
        rig.log.append(kind="message", text="from matrix", source=comms.Source("matrix", author="@a:x", ref="$e"))
        rig.agent("message", "after")
        rig.wait_until(lambda: any(s[2] == "after" for s in rig.matrix.sent))
        assert [s[2] for s in rig.matrix.sent if s[3] == "m.text"] == ["after"]

    def test_an_inbound_image_lands_in_the_upload_store(self, tmp_path):
        store = tmp_path / "uploads"
        store.mkdir()
        rig = Rig(tmp_path, uploads=str(store))
        rig.matrix.media["mxc://example.org/pic"] = (b"\x89PNG\r\n\x1a\n", "image/png")
        rig.connector.replay()
        rig.matrix.human_says("shot.png", msgtype="m.image", url="mxc://example.org/pic", info={"mimetype": "image/png"})
        assert rig.connector.sync_once() == 1
        record = rig.log.read_since(0)[0][-1]
        (attachment,) = record.attachments
        assert attachment["media_type"] == "image/png" and attachment["path"].startswith("/home/developer/.lmer-uploads/")
        assert record.text.startswith("[lmer upload] /home/developer/.lmer-uploads/")
        assert len(list(store.iterdir())) == 1
        rig.connector.stop()

    def test_an_inbound_file_is_written_store_relative_not_by_name(self, tmp_path, monkeypatch):
        """#416: this path wrote with `write_bytes` + `os.chmod` on a name it had
        chosen a moment earlier, in a directory the container mounts `rw`. A
        symlink planted at that name took the write and the mode. It goes through
        `stage_upload`'s `O_EXCL` create now, which takes the next name instead."""
        from lmer_cli import thread_access

        store = tmp_path / "uploads"
        store.mkdir()
        victim = tmp_path / "victim"
        # The name `stage_upload` is about to choose, made a symlink to somewhere
        # outside the store — the race, made deterministic. The clock is replaced
        # only as `thread_access` sees it: patching the module's own `strftime`
        # would freeze it for everything else in the process too.
        class _FrozenClock:
            def __getattr__(self, name):
                return getattr(time, name)

            def strftime(self, fmt, *args):
                return "20260905-000000"

        monkeypatch.setattr(thread_access, "time", _FrozenClock())
        # And on the connector too, if it ever composes a name itself again:
        # without this the pre-fix code picked a real timestamp, the planted
        # symlink was never the name it chose, and this test passed against the
        # very code it exists to catch.
        monkeypatch.setattr(connector, "time", _FrozenClock(), raising=False)
        (store / "20260905-000000-shot.png").symlink_to(victim)

        rig = Rig(tmp_path, uploads=str(store))
        rig.matrix.media["mxc://example.org/pic"] = (b"\x89PNG\r\n\x1a\n", "image/png")
        rig.connector.replay()
        rig.matrix.human_says(
            "shot.png", msgtype="m.image", url="mxc://example.org/pic",
            info={"mimetype": "image/png"},
        )
        assert rig.connector.sync_once() == 1
        try:
            assert victim.exists() is False, "the write followed the symlink out of the store"
            record = rig.log.read_since(0)[0][-1]
            (attachment,) = record.attachments
            written = store / Path(attachment["path"]).name
            assert written.is_symlink() is False
            assert written.read_bytes() == b"\x89PNG\r\n\x1a\n"
            assert oct(written.stat().st_mode)[-3:] == "600"
        finally:
            rig.connector.stop()

    def test_an_inbound_file_over_the_cap_is_not_stored(self, tmp_path):
        """#416: the download was unbounded, and the file is posted by whoever is
        in the room rather than by anyone here."""
        from lmer_cli import thread_access

        store = tmp_path / "uploads"
        store.mkdir()
        rig = Rig(tmp_path, uploads=str(store))
        rig.matrix.media["mxc://example.org/dump"] = (
            b"x" * (thread_access.MAX_FILE_BYTES + 1), "application/octet-stream",
        )
        rig.connector.replay()
        rig.matrix.human_says(
            "core.dump", msgtype="m.file", url="mxc://example.org/dump",
            info={"mimetype": "application/octet-stream"},
        )
        assert rig.connector.sync_once() == 1
        try:
            record = rig.log.read_since(0)[0][-1]
            assert not record.attachments
            assert "not stored" in record.text
            assert list(store.iterdir()) == []
        finally:
            rig.connector.stop()

    def test_a_declared_size_over_the_cap_is_not_downloaded_at_all(self, tmp_path):
        store = tmp_path / "uploads"
        store.mkdir()
        rig = Rig(tmp_path, uploads=str(store))
        rig.connector.replay()
        rig.matrix.human_says(
            "core.dump", msgtype="m.file", url="mxc://example.org/never-fetched",
            info={"mimetype": "application/octet-stream", "size": 4 * 1024 * 1024 * 1024},
        )
        # The mxc is in no `media` map: reaching the download would KeyError.
        assert rig.connector.sync_once() == 1
        try:
            assert list(store.iterdir()) == []
        finally:
            rig.connector.stop()

    def test_an_outbound_attachment_needs_the_media_assertion(self, tmp_path):
        store = tmp_path / "uploads"
        store.mkdir()
        (store / "report.txt").write_text("all green", encoding="utf-8")
        rig = Rig(tmp_path, uploads=str(store))
        rig.connector.room_id = ROOM
        record = {"kind": "message", "text": "see", "source": {"channel": "agent"}, "meta": {},
                  "attachments": [{"name": "report.txt", "media_type": "text/plain", "bytes": 9,
                                   "path": "/home/developer/.lmer-uploads/report.txt"}]}
        rig.connector._post_outbound(record, {})
        assert rig.matrix.uploads == []
        assert any("withheld" in s[2] and "LMER_MATRIX_AUTHENTICATED_MEDIA" in s[2] for s in rig.matrix.sent)

        asserted = Rig(tmp_path, uploads=str(store), authenticated_media=True)
        from dataclasses import replace
        asserted.connector.config = replace(asserted.connector.config, thread_event="$separate-media-policy-case")
        asserted.connector.room_id = ROOM
        asserted.connector._post_outbound(record, {})
        assert asserted.matrix.uploads == [("report.txt", "text/plain", b"all green")]
        assert any(s[3] == "m.file" for s in asserted.matrix.sent)


# ---------------------------------------------------------------------------
# configuration, the target handler, the listener
# ---------------------------------------------------------------------------


class TestConfigAndTargets:
    def test_from_env_names_what_is_missing(self):
        with pytest.raises(ValueError, match="LMER_MATRIX_ACCESS_TOKEN"):
            connector.ConnectorConfig.from_env({
                "LMER_FASTAPI_PORT": "1", "LMER_FASTAPI_TOKEN": "t",
                "LMER_MATRIX_ROOM_ID": ROOM, "LMER_MATRIX_THREAD_EVENT": ROOT,
                "LMER_MATRIX_HOMESERVER": "https://hs", "LMER_MATRIX_USER_ID": BOT,
            })
        config = connector.ConnectorConfig.from_env({
            "LMER_FASTAPI_PORT": "1", "LMER_FASTAPI_TOKEN": "t",
            "LMER_MATRIX_ROOM_ID": ROOM, "LMER_MATRIX_THREAD_EVENT": ROOT,
            "LMER_MATRIX_HOMESERVER": "https://hs", "LMER_MATRIX_USER_ID": BOT,
            "LMER_MATRIX_ACCESS_TOKEN": "syt", "LMER_MATRIX_AUTHENTICATED_MEDIA": "1",
        })
        assert config.authenticated_media is True and "syt" not in repr(config)

    def test_the_target_handler_claims_matrix_links_and_keeps_the_token_out(self, monkeypatch):
        link = permalink.matrix_to_link(ROOM, ROOT)
        repo, handlers = targets.partition_targets([link, "https://gitlab.example.com/a/b"])
        assert repo == ["https://gitlab.example.com/a/b"]
        (handler,) = handlers
        assert isinstance(handler, targets.MatrixThreadTargets)
        assert (handler.room, handler.thread_event) == (ROOM, ROOT)
        monkeypatch.delenv("LMER_MATRIX_ACCESS_TOKEN", raising=False)
        assert "LMER_MATRIX_ACCESS_TOKEN" in handler.validate_environment()
        monkeypatch.setenv("LMER_MATRIX_HOMESERVER", "https://hs")
        monkeypatch.setenv("LMER_MATRIX_USER_ID", BOT)
        monkeypatch.setenv("LMER_MATRIX_ACCESS_TOKEN", "syt")
        assert handler.validate_environment() is None
        env = targets.special_target_env(handlers)
        assert env["LMER_MATRIX_ACCESS_TOKEN"] is None and env["LMER_CHAT_CHANNEL"] == "matrix"
        assert env["LMER_MATRIX_ROOM_ID"] == ROOM and env["SLACK_BOT_TOKEN"] is None
        assert handler.supports_repoless_session("chat")

    def test_the_handler_without_a_control_plane_does_not_claim_the_thread(self, tmp_path, monkeypatch):
        monkeypatch.setattr(registry, "REGISTRY_DIR", str(tmp_path))
        handler = targets.MatrixThreadTargets()
        handler.add(permalink.matrix_to_link(ROOM, ROOT))
        handler.on_session_start()
        assert not registry.is_thread_connected(ROOM, ROOT, kind="matrix")
        assert not registry.is_thread_connected(ROOM, ROOT)
        handler.on_session_end()
        assert not registry.is_thread_connected(ROOM, ROOT, kind="matrix")

    def test_listener_denies_unlisted_senders_and_finds_mentions(self, monkeypatch):
        config = listener.ListenerConfig({
            "LMER_MATRIX_HOMESERVER": "https://hs", "LMER_MATRIX_USER_ID": BOT,
            "LMER_MATRIX_ACCESS_TOKEN": "syt", "LMER_MATRIX_ALLOWED_USERS": "@tester:example.org",
        })
        fake = FakeMatrix()

        class Sessions:
            max_sessions = 5

        watcher = listener.Listener(config, Sessions(), matrix=fake)
        assert watcher.sync_once() == []  # the initial sync is history
        fake.pending.append({"event_id": "$m1", "sender": "@tester:example.org", "type": "m.room.message",
                             "content": {"body": "@lmer start", "msgtype": "m.text"}})
        fake.pending.append({"event_id": "$m2", "sender": "@stranger:evil.org", "type": "m.room.message",
                             "content": {"body": "@lmer start", "msgtype": "m.text"}})
        fake.pending.append({"event_id": "$m3", "sender": "@tester:example.org", "type": "m.room.message",
                             "content": {"body": "no mention here", "msgtype": "m.text"}})
        found = watcher.sync_once()
        assert [(room, event["event_id"]) for room, event in found] == [(ROOM, "$m1")]

    def test_listener_unset_list_and_case_variant_are_refused(self, caplog):
        account = {
            "LMER_MATRIX_HOMESERVER": "https://hs", "LMER_MATRIX_USER_ID": BOT,
            "LMER_MATRIX_ACCESS_TOKEN": "syt",
        }

        class Sessions:
            max_sessions = 5

        for allowed in (None, "@tester:example.org"):
            config = listener.ListenerConfig({
                **account, **({} if allowed is None else {"LMER_MATRIX_ALLOWED_USERS": allowed}),
            })
            fake = FakeMatrix()
            watcher = listener.Listener(config, Sessions(), matrix=fake)
            assert watcher.sync_once() == []
            fake.pending.append({
                "event_id": "$m1", "sender": "@Tester:example.org",
                "type": "m.room.message",
                "content": {"body": "@lmer start", "msgtype": "m.text"},
            })
            with caplog.at_level("INFO", logger="matrix_chat.listener"):
                assert watcher.sync_once() == []
        assert "LMER_MATRIX_ALLOWED_USERS" in caplog.text

    def test_empty_list_startup_explains_fatal_exit_at_error_log_level(
        self, monkeypatch, capsys
    ):
        from lmer_cli import runtime, tls

        monkeypatch.setattr(runtime, "apply_env_file_defaults", lambda *args, **kwargs: None)
        monkeypatch.setattr(tls, "ensure_ca_bundle", lambda: None)
        monkeypatch.setenv("LMER_MATRIX_HOMESERVER", "https://hs")
        monkeypatch.setenv("LMER_MATRIX_USER_ID", BOT)
        monkeypatch.setenv("LMER_MATRIX_ACCESS_TOKEN", "syt")
        monkeypatch.setenv("LMER_MATRIX_ALLOWED_USERS", "")

        assert listener.main(["--log-level", "ERROR"]) == 1

        assert "LMER_MATRIX_ALLOWED_USERS" in capsys.readouterr().err

    def test_listener_config_requires_the_account(self):
        with pytest.raises(ValueError, match="LMER_MATRIX_HOMESERVER"):
            listener.ListenerConfig({})

    def test_the_console_scripts_are_installed(self):
        from pathlib import Path

        scripts = Path(__file__).resolve().parents[1].joinpath("pyproject.toml").read_text(encoding="utf-8")
        for name in ("lmer-matrix-listener", "lmer-matrix-connector", "lmer-slack-connector"):
            assert f"{name} = " in scripts


@pytest.mark.parametrize("reply", ["2", "the other one"])
def test_explicit_reply_targets_newer_question_before_oldest_options(rig, reply):
    rig.connector.replay()
    older = rig.agent("question", "deploy?", options=["yes", "no"])
    newer = rig.agent("question", "branch?", options=["main", "prep"])
    rig.connector._outbound_once()
    rig.matrix.human_says(reply, **{"m.relates_to": {
        "rel_type": "m.thread", "event_id": ROOT, "is_falling_back": False,
        "m.in_reply_to": {"event_id": rig.connector._question_events[newer.id]},
    }})
    assert rig.connector.sync_once() == 1
    answer = rig.log.read_since(newer.seq)[0][-1]
    assert answer.in_reply_to == newer.id
    assert answer.text == ("prep" if reply == "2" else reply)
    assert rig.log.question(older.id).open


def test_matrix_failed_bus_post_retries_same_sync_and_free_answer(rig, monkeypatch):
    rig.connector.replay()
    question = rig.agent("question", "branch?")
    event = rig.matrix.human_says("prep-release")
    previous = rig.connector._next_batch
    calls = []
    def sync(**kwargs):
        calls.append(kwargs.get("since"))
        return {"next_batch": "next", "rooms": {"join": {ROOM: {"timeline": {"events": [event]}}}}}
    monkeypatch.setattr(rig.matrix, "sync", sync)
    with monkeypatch.context() as patcher:
        def fail(*args, **kwargs):
            raise bus.BusError("temporary bus refusal")
        patcher.setattr(rig.connector.bus, "post", fail)
        with pytest.raises(bus.BusError, match="temporary"):
            rig.connector.sync_once()
    assert rig.connector.sync_once() == 1
    assert calls == [previous, previous]
    answer = rig.log.read_since(question.seq)[0][-1]
    assert (answer.kind, answer.text, answer.in_reply_to) == ("answer", "prep-release", question.id)


def test_thread_fallback_relation_does_not_claim_to_be_an_explicit_reply(rig):
    rig.connector.replay()
    question = rig.agent("question", "branch?", options=["main", "prep"])
    rig.connector._post_outbound(question.to_dict(), {})
    rig.matrix.human_says("hold on", **{"m.relates_to": {
        "rel_type": "m.thread", "event_id": ROOT, "is_falling_back": True,
        "m.in_reply_to": {"event_id": rig.connector._question_events[question.id]},
    }})
    assert rig.connector.sync_once() == 1
    assert rig.log.question(question.id).open
    assert rig.log.read_since(question.seq)[0][-1].kind == "message"


def test_initial_sync_does_not_drop_a_reply_after_the_thread_snapshot(rig):
    event = rig.matrix.human_says("arrived during startup")
    assert rig.connector.replay() == 1
    (record,) = rig.log.read_since(0)[0]
    assert record.source.ref == event["event_id"]
    assert "has 2 earlier messages" in record.text
    assert "arrived during startup" not in record.text
    assert rig.connector.sync_once() == 0


def test_matrix_notice_counts_the_full_thread_and_downloads_nothing(tmp_path, monkeypatch):
    from matrix_chat.permalink import matrix_to_link
    from unittest.mock import Mock

    store = tmp_path / "uploads"
    store.mkdir()
    rig = Rig(tmp_path, uploads=str(store))
    for index in range(125):
        rig.matrix.human_says(f"stale request {index}", live=False)
    media = rig.matrix.human_says(
        "shot.png", live=False, msgtype="m.image", url="mxc://example.org/shot",
    )
    download = Mock(return_value=(b"image", "image/png"))
    monkeypatch.setattr(rig.matrix, "download", download)
    fetch = Mock(wraps=rig.matrix.thread_events)
    monkeypatch.setattr(rig.matrix, "thread_events", fetch)
    assert rig.connector.replay() == 1
    fetch.assert_called_once_with(ROOM, ROOT, limit=None)
    (record,) = rig.log.read_since(0)[0]
    assert len(record.text.splitlines()) == 1
    assert "has 127 earlier messages" in record.text
    assert matrix_to_link(ROOM, ROOT) in record.text and "--limit 127" in record.text
    assert "1970-01-01T00:00:00Z .. 1970-01-01T00:00:00Z" in record.text
    assert record.source.ref == media["event_id"]
    assert not record.attachments and list(store.iterdir()) == []
    assert "stale request" not in record.text and "[lmer upload]" not in record.text
    download.assert_not_called()
    rig.matrix.human_says("after attach")
    assert rig.connector.sync_once() == 1
    assert rig.log.read_since(record.seq)[0][-1].text == "after attach"
    rig.connector.stop()


def test_matrix_root_only_and_unchanged_restart(rig):
    assert rig.connector.replay() == 1
    (record,) = rig.log.read_since(0)[0]
    assert "has 1 earlier message" in record.text and record.source.ref == ROOT
    rig.connector.stop()
    fresh = connector.MatrixConnector(rig.config, matrix=rig.matrix, bus_client=rig.bus)
    rig.connector = fresh
    assert fresh.replay() == 1
    assert rig.log.seq == record.seq
    newest = rig.matrix.human_says("while down", live=False)
    rig.connector.stop()
    again = connector.MatrixConnector(rig.config, matrix=rig.matrix, bus_client=rig.bus)
    rig.connector = again
    assert again.replay() == 1
    (notice,) = rig.log.read_since(record.seq)[0]
    assert "has 2 earlier messages" in notice.text and notice.source.ref == newest["event_id"]


def test_matrix_bot_only_thread_gets_a_notice(rig):
    rig.matrix.root["sender"] = BOT
    assert rig.connector.replay() == 1
    (record,) = rig.log.read_since(0)[0]
    assert "has 1 earlier message" in record.text
    assert "@lmer help me" not in record.text


def test_matrix_uncapped_history_follows_all_pages(monkeypatch):
    from matrix_chat.client import MatrixAccount, MatrixClient

    client = MatrixClient(MatrixAccount("https://hs.example.org", BOT, "test-token"))
    calls = []

    pages = [
        {"chunk": [{"event_id": str(i), "origin_server_ts": i} for i in range(100)], "next_batch": "page2"},
        {"chunk": [{"event_id": "100", "origin_server_ts": 100}], "next_batch": None},
    ]
    def fetch(method, path, *, params):
        calls.append(dict(params))
        return pages[len(calls) - 1]
    monkeypatch.setattr(client, "_call", fetch)
    assert len(client.thread_events(ROOM, ROOT, limit=None)) == 101
    assert calls == [{"limit": 100, "dir": "f"}, {"limit": 100, "dir": "f", "from": "page2"}]
    calls.clear()
    assert len(client.thread_events(ROOM, ROOT)) == 100
    assert len(calls) == 1, "the existing bounded read behavior is unchanged"


def test_matrix_replayed_history_is_never_an_answer_and_leaves_the_free_slot(rig):
    """The backlog predates the attach, so it answers nothing and does not spend
    the first-free-text slot — the next real message still can (issue #426)."""
    question = rig.agent("question", "what should I call it?")
    rig.matrix.human_says("morning", live=False)
    assert rig.connector.replay() == 1
    replayed = rig.log.read_since(question.seq)[0][-1]
    assert (replayed.kind, replayed.in_reply_to) == ("message", None)
    assert rig.log.question(question.id).open
    rig.matrix.human_says("call it widget")
    assert rig.connector.sync_once() == 1
    answer = rig.log.read_since(replayed.seq)[0][-1]
    assert (answer.kind, answer.text, answer.in_reply_to) == (
        "answer", "call it widget", question.id,
    )


class TestPostsFromOtherRuns:
    """An event the bot account sent is not automatically this connector's own
    echo (#427): the account is shared, so who posted it is read from the
    event's own `net.20c.lmer.session` content key, never from its text."""

    def test_another_runs_post_is_relayed_with_its_run_as_the_author(self, rig):
        rig.connector.replay()
        event = rig.matrix.app_says(
            "[lmer session s-other] have a look at !288",
            poster=thread_posts.Poster.for_session("s-other", "chat-mr-288"),
        )
        assert rig.connector.sync_once() == 1
        record = rig.log.read_since(1)[0][0]
        assert record.source == comms.Source(
            "matrix", author="lmer chat-mr-288", ref=event["event_id"],
        )
        assert record.text == "[lmer session s-other] have a look at !288"
        assert b"[matrix \xc2\xb7 lmer chat-mr-288]" in b"".join(rig.sink)

    def test_the_platforms_own_post_is_relayed_as_the_platform(self, rig):
        rig.connector.replay()
        rig.matrix.app_says("[operator] look at the pipeline",
                            poster=thread_posts.Poster.for_platform("operator"))
        assert rig.connector.sync_once() == 1
        assert rig.log.read_since(1)[0][0].source.author == "platform \u00b7 operator"

    def test_this_sessions_own_post_is_still_skipped(self, rig):
        rig.connector.replay()
        rig.matrix.app_says("[lmer session sess-1] posted elsewhere",
                            poster=thread_posts.Poster.for_session("sess-1", "this run"))
        assert rig.connector.sync_once() == 0

    def test_an_unstamped_event_is_skipped_however_its_text_reads(self, rig):
        rig.connector.replay()
        rig.matrix.app_says("[lmer session s-other] a text prefix is not evidence")
        assert rig.connector.sync_once() == 0
        assert rig.log.read_since(1)[0] == []

    def test_a_human_in_the_same_thread_is_untouched(self, rig):
        rig.connector.replay()
        rig.matrix.app_says("from another run",
                            poster=thread_posts.Poster.for_session("s-other", "chat-mr-288"))
        rig.matrix.human_says("and one from me")
        assert rig.connector.sync_once() == 2
        authors = [record.source.author for record in rig.log.read_since(1)[0]]
        assert authors == ["lmer chat-mr-288", "@tester:example.org"]

    def test_it_is_not_mirrored_straight_back_into_the_thread(self, rig):
        """A live inbound record is never mirrored back to its own channel."""
        rig.connector.start()
        rig.wait_until(lambda: any(
            record.text.startswith("[replay notice]") for record in rig.log.read_since(0)[0]
        ))
        rig.matrix.app_says("[lmer session s-other] have a look",
                            poster=thread_posts.Poster.for_session("s-other", "chat-mr-288"))
        rig.wait_until(lambda: any(
            "have a look" in record.text for record in rig.log.read_since(0)[0]
        ))
        rig.agent("message", "after")
        rig.wait_until(lambda: any(sent[2] == "after" for sent in rig.matrix.sent))
        assert not any("have a look" in sent[2] for sent in rig.matrix.sent)

    def test_another_runs_post_can_answer_an_open_question(self, rig):
        """§6 binding applies to it exactly as to a human message."""
        rig.connector.replay()
        question = rig.agent("question", "branch?", options=["main", "prep-release"])
        rig.matrix.app_says("2", poster=thread_posts.Poster.for_session("s-other", "chat-mr-288"))
        assert rig.connector.sync_once() == 1
        answer = rig.log.read_since(question.seq)[0][0]
        assert (answer.kind, answer.text, answer.in_reply_to) == (
            "answer", "prep-release", question.id,
        )
        assert answer.source.author == "lmer chat-mr-288"

    def test_reconnect_counts_both_sides_without_author_lines(self, rig):
        rig.connector.replay()
        rig.matrix.human_says("working on it", sender=BOT, live=False)
        rig.matrix.app_says("have a look", live=False,
                            poster=thread_posts.Poster.for_session("s-other", "chat-mr-288"))
        rig.connector.stop()
        reconnected = connector.MatrixConnector(rig.config, matrix=rig.matrix, bus_client=rig.bus)
        rig.connector = reconnected
        assert reconnected.replay() == 1
        notice = rig.log.read_since(0)[0][-1]
        assert "has 3 earlier messages" in notice.text
        assert "working on it" not in notice.text and "chat-mr-288" not in notice.text
        rig.matrix.app_says("own output")
        rig.matrix.app_says("new post", poster=thread_posts.Poster.for_session("s-other", "chat-mr-288"))
        assert reconnected.sync_once() == 1
        assert rig.log.read_since(0)[0][-1].source.author == "lmer chat-mr-288"

    def test_the_mirror_carries_the_session_stamp_for_live_attribution(self, rig):
        """The criterion's third stamping site — see the Slack twin."""
        rig.connector.replay()
        record = rig.agent("message", "working on it")
        assert rig.connector._post_outbound(record.to_dict(), {})
        assert thread_posts.poster_from_matrix(
            rig.matrix.stamps[-1]
        ) == thread_posts.Poster.for_session("sess-1")

    def test_the_hold_ends_and_says_so_again_after_the_interval(self, rig, caplog):
        now = [0.0]
        held = connector.MatrixConnector(
            rig.config, matrix=rig.matrix, bus_client=rig.bus, clock=lambda: now[0],
        )
        held.replay()
        rig.matrix.app_says("nobody stamped this")
        rig.matrix.app_says("nor this")
        assert held.sync_once() == 0
        now[0] = thread_posts.GRACE_SECONDS + 1
        with caplog.at_level("WARNING"):
            assert held.sync_once() == 0
        lines = [r for r in caplog.records if "unattributed_post" in r.getMessage()]
        assert len(lines) == 1
        assert "docs/COMMS.md" in lines[0].getMessage()


@pytest.mark.parametrize("resuming", [False, True])
@pytest.mark.parametrize("launch, expected", [
    ({"preset": "astra", "model": "gpt-test"}, " Preset: astra; model: gpt-test."),
    ({"preset": None, "model": None}, " Preset: none; model: harness default."),
    ({"preset": "a<&b", "model": "<model>\nname"}, " Preset: a<&b; model: <model> name."),
    (None, ""),
])
def test_attachment_notice_names_launch_settings_as_plain_text(tmp_path, launch, expected, resuming):
    from dataclasses import replace

    rig = Rig(tmp_path, launch_settings=launch)
    rig.connector.config = replace(rig.config, attach_ack=True)
    rig.connector.connect()
    if resuming:
        rig.agent("message", "already read")
        rig.bus.ack(rig.connector.subscriber, 1)
    try:
        rig.connector._seed_cursor()
        assert rig.connector._bus_cursor == (1 if resuming else 0)
        rig.connector._announce_attached()
        rig.connector._announce_attached()
        assert rig.matrix.sent == [(ROOM, ROOT, connector.ATTACH_ACK + expected, "m.notice")]
        assert rig.matrix.stamps == [{"net.20c.lmer.connection": True}]
    finally:
        rig.log.close()


def test_attachment_settings_do_not_override_matrix_ack_off(tmp_path):
    rig = Rig(tmp_path, launch_settings={"preset": "astra", "model": "gpt-test"})
    try:
        rig.connector._seed_cursor()
        rig.connector._announce_attached()
        assert rig.matrix.sent == []
    finally:
        rig.log.close()


def test_matrix_attachment_notice_waits_for_supervisor_identity(tmp_path, monkeypatch):
    from dataclasses import replace

    rig = Rig(tmp_path)
    rig.connector.config = replace(rig.config, attach_ack=True)
    rig.connector.connect()

    def unavailable():
        raise bus.BusError("health unavailable")

    try:
        with monkeypatch.context() as fault:
            fault.setattr(rig.bus, "health", unavailable)
            with pytest.raises(bus.BusError, match="health unavailable"):
                rig.connector._seed_cursor()
            assert rig.matrix.sent == []
        rig.connector._seed_cursor()
        rig.connector._announce_attached()
        assert rig.matrix.sent == [(ROOM, ROOT, connector.ATTACH_ACK, "m.notice")]
    finally:
        rig.connector.stop(timeout=DRAIN)
        rig.bus._session._http.close()
        rig.log.close()
