"""Reading and posting a thread the session is not connected to (issue #414).

The host side of `lmer-signal read` / `post --thread`: what a read reports, what
a post says it is, and the two boundaries that matter — a credential that stays
on the host, and a file the session may name only inside its own upload store.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from ask_channel import bus
from lmer_cli import thread_access, thread_posts
from lmer_cli.thread_access import (
    CLI_ENVIRONMENT, ThreadServicer, UploadStore, open_thread, post_thread,
    read_from_store, read_thread, render_record, resolve_thread, stored_name_of,
)
from lmer_cli.threads import ThreadError, parse_channel, parse_thread

SLACK_LINK = "https://example.slack.com/archives/C0123ABCDEF/p1712345678000900"
SLACK_ENV = {"SLACK_BOT_TOKEN": "xoxb-test"}
MATRIX_LINK = "https://matrix.to/#/!room:example.org/$root"
MATRIX_ENV = {
    "LMER_MATRIX_HOMESERVER": "https://matrix.example.org",
    "LMER_MATRIX_USER_ID": "@lmer:example.org",
    "LMER_MATRIX_ACCESS_TOKEN": "syt-test",
}


class FakeSlack:
    bot_token = "xoxb-test"

    def __init__(self, messages=None, *, fail=None, code=None, scopes=None):
        self.messages = messages if messages is not None else []
        self.fail = fail
        self.code = code
        self.scopes = (
            scopes if scopes is not None else ["chat:write", "channels:history"]
        )
        self.posted = []
        self.opened = []
        self.calls = 0

    def get_replies(self, channel, thread_ts, oldest=None):
        """Slack's own shape: given a message inside a thread it answers with
        that message, whose `thread_ts` names the root."""
        self.calls += 1
        self._maybe_fail()
        return list(self.messages)

    def _maybe_fail(self):
        if not self.fail:
            return
        from slack_chat.client import SlackError

        raise SlackError(self.fail, code=self.code)

    def post_message(self, channel, thread_ts, text):
        self._maybe_fail()
        self.posted.append((channel, thread_ts, text))
        return "1712345679.000100"

    def workspace(self):
        """``auth.test``'s answer: the host, and the token's granted scopes as
        the ``x-oauth-scopes`` header states them (``None`` for a header that was
        not there). Outside ``fail``'s reach on purpose: that knob stands for the
        conversation refusing, which is what opening a thread has to report, and
        a fake that failed here first would report it for the wrong call."""
        return {"host": "example.slack.com", "scopes": self.scopes}

    def post_root(self, channel, text):
        self._maybe_fail()
        self.opened.append((channel, text))
        return {"channel": "C0123ABCDEF", "ts": "1712345678.000900"}

    def _request(self, method, endpoint, params=None):
        assert endpoint == "users.info"
        return {"user": {"profile": {"display_name": f"name-of-{params['user']}"}}}


class FakeMatrix:
    def __init__(self, *, encrypted=False, events=None, joined=None):
        self.encrypted = encrypted
        self.events = events or []
        self.joined = (
            joined if joined is not None
            else ["!room:example.org", "!resolved:example.org"]
        )
        self.sent = []
        self.stamps = []
        self.uploaded = []

    def resolve_room(self, room):
        return "!resolved:example.org" if room.startswith("#") else room

    def room_is_encrypted(self, room_id):
        """The real client's shape: this is a room-state read, and a homeserver
        answers a non-member 403 for it (``MatrixClient.room_is_encrypted``
        re-raises everything but a 404). A fake that answered anyway is how a
        membership refusal became a test that could not fail."""
        if room_id not in self.joined:
            from matrix_chat.client import MatrixError

            raise MatrixError(
                "GET /state/m.room.encryption → 403 M_FORBIDDEN You aren't a "
                "member of the room",
                status=403, errcode="M_FORBIDDEN",
            )
        return self.encrypted

    def joined_rooms(self):
        return list(self.joined)

    def send_message(self, room_id, content):
        """A root event, which `open` sends rather than `send_thread_text`. Its
        stamp is recorded like every other send's: a root nothing attributes is
        the finding #429 answered on top of #427."""
        self.sent.append((content.get("body"), content.get("msgtype")))
        self.stamps.append({
            key: value for key, value in content.items()
            if key not in ("body", "msgtype")
        })
        return "$root"

    def display_name(self, user_id):
        return f"name-of-{user_id}"

    def get_event(self, room_id, event_id):
        return {
            "event_id": event_id, "sender": "@alice:example.org",
            "origin_server_ts": 1712345678000, "content": {"body": "the root"},
        }

    def thread_events(self, room_id, root_event_id, limit=100):
        return self.events

    def send_thread_text(self, room_id, root, text, last_event_id=None, msgtype="m.text",
                         extra=None):
        self.sent.append((text, msgtype))
        self.stamps.append(extra or {})
        return "$posted"

    def send_thread_file(self, room_id, root, *, mxc, name, media_type, size,
                         last_event_id=None, extra=None):
        self.uploaded.append((mxc, name, size))
        self.stamps.append(extra or {})
        return "$file"

    def upload(self, data, *, content_type, filename):
        return f"mxc://example.org/{filename}"

    def download(self, mxc):
        return b"file bytes", "text/plain"


class _SlackDownload:
    """What `requests.get(..., stream=True)` gives the fetcher back."""

    def __init__(self, body: bytes, *, declared=None):
        self.body = body
        self.headers = {"Content-Type": "text/plain"}
        if declared is not None:
            self.headers["Content-Length"] = str(declared)
        self.closed = False
        self.read = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=65536):
        for start in range(0, len(self.body), chunk_size):
            chunk = self.body[start:start + chunk_size]
            self.read += len(chunk)
            yield chunk


@pytest.fixture
def slack(monkeypatch):
    client = FakeSlack(messages=[
        {"ts": "1712345678.000900", "user": "U1", "text": "the root question"},
        {"ts": "1712345678.000901", "user": "U2", "text": "a reply"},
    ])
    monkeypatch.setattr(thread_access, "_slack_client", lambda token: client)
    return client


@pytest.fixture
def matrix(monkeypatch):
    client = FakeMatrix(events=[{
        "event_id": "$reply", "sender": "@bob:example.org",
        "origin_server_ts": 1712345679000, "content": {"body": "a threaded reply"},
    }])
    monkeypatch.setattr(thread_access, "_matrix_client", lambda account: client)
    return client


@pytest.fixture(autouse=True)
def registry_dir(tmp_path, monkeypatch):
    """Where a Slack post records which run made it (#427)."""
    monkeypatch.setattr(thread_posts, "REGISTRY_DIR", str(tmp_path / "thread-posts"))


@pytest.fixture
def store(tmp_path):
    host = tmp_path / "uploads"
    host.mkdir()
    return UploadStore(host_dir=str(host), container_dir="/home/developer/.lmer-uploads")


class TestCredentials:
    def test_a_missing_slack_token_names_the_process_whose_environment_it_is(self):
        with pytest.raises(ThreadError) as caught:
            resolve_thread(SLACK_LINK, env={}, where=CLI_ENVIRONMENT)
        assert "SLACK_BOT_TOKEN" in str(caught.value)
        assert CLI_ENVIRONMENT in str(caught.value)

    def test_a_missing_matrix_account_names_every_variable_it_needs(self):
        with pytest.raises(ThreadError) as caught:
            resolve_thread(MATRIX_LINK, env={}, where=CLI_ENVIRONMENT)
        for name in MATRIX_ENV:
            assert name in str(caught.value)

    def test_a_link_that_names_no_thread_is_refused_before_any_credential(self):
        with pytest.raises(ThreadError):
            resolve_thread("https://example.com/not-a-thread", env=SLACK_ENV)


class TestReading:
    def test_a_slack_thread_reads_as_bus_style_records(self, slack):
        payload = read_thread(SLACK_LINK, env=SLACK_ENV)
        assert payload["thread"]["channel"] == "slack"
        assert [record["text"] for record in payload["messages"]] == [
            "the root question", "a reply",
        ]
        first = payload["messages"][0]
        assert first["author"] == "name-of-U1"
        assert first["ref"] == "C0123ABCDEF:1712345678.000900:1712345678.000900"
        assert first["at"].startswith("2024-")
        assert render_record(first) == "[slack · name-of-U1] the root question"

    def test_a_read_asks_slack_for_the_thread_once(self, slack):
        """The resolution already fetched it. Asking again for the same
        conversation — the ordinary case — spent two calls and two rate-limit
        tokens, and a 429 is paid for in `time.sleep` inside the servicer."""
        slack.calls = 0
        read_thread(SLACK_LINK, env=SLACK_ENV)
        assert slack.calls == 1

    def test_only_the_last_messages_are_read(self, slack):
        payload = read_thread(SLACK_LINK, env=SLACK_ENV, limit=1)
        assert [record["text"] for record in payload["messages"]] == ["a reply"]

    def test_a_link_to_a_message_inside_a_thread_reads_its_thread(self, monkeypatch):
        """Slack names the root the link did not: `conversations.replies` on a
        message with no replies of its own returns that message alone."""
        client = FakeSlack(messages=[{
            "ts": "1712345678.000900", "thread_ts": "1712345600.000100",
            "user": "U1", "text": "a reply, linked directly",
        }])
        monkeypatch.setattr(thread_access, "_slack_client", lambda token: client)
        ref, _client, fetched = resolve_thread(SLACK_LINK, env=SLACK_ENV)
        assert ref.key["thread_ts"] == "1712345600.000100"
        # Nothing to reuse: what was fetched is the wrong conversation.
        assert fetched is None

    def test_a_matrix_thread_reads_its_root_and_its_replies(self, matrix):
        payload = read_thread(MATRIX_LINK, env=MATRIX_ENV)
        assert [record["text"] for record in payload["messages"]] == [
            "the root", "a threaded reply",
        ]
        assert payload["messages"][1]["author"] == "name-of-@bob:example.org"

    def test_an_encrypted_room_is_refused_by_name(self, monkeypatch):
        client = FakeMatrix(encrypted=True)
        monkeypatch.setattr(thread_access, "_matrix_client", lambda account: client)
        with pytest.raises(ThreadError) as caught:
            read_thread(MATRIX_LINK, env=MATRIX_ENV)
        assert "!room:example.org" in str(caught.value)
        assert "encrypted" in str(caught.value)

    def test_an_error_that_merely_mentions_a_code_is_not_read_as_that_code(self, monkeypatch):
        """The verdict sets the status the platform route answers with, so it is
        read off Slack's own `error` field rather than matched in prose that is
        composed three different ways and is nobody's contract."""
        client = FakeSlack(fail="Request failed: connect to debug=channel_not_found_host")
        monkeypatch.setattr(thread_access, "_slack_client", lambda token: client)
        with pytest.raises(ThreadError) as caught:
            read_thread(SLACK_LINK, env=SLACK_ENV)
        assert caught.value.status == 502
        assert "invite the bot" not in str(caught.value)

    def test_a_channel_the_token_cannot_see_says_what_to_do_about_it(self, monkeypatch):
        client = FakeSlack(fail="Slack API error: channel_not_found", code="channel_not_found")
        monkeypatch.setattr(thread_access, "_slack_client", lambda token: client)
        with pytest.raises(ThreadError) as caught:
            read_thread(SLACK_LINK, env=SLACK_ENV)
        assert "invite the bot" in str(caught.value)
        assert caught.value.status == 403


class TestFiles:
    def test_a_thread_file_is_copied_in_and_named_by_its_container_path(self, monkeypatch, store, tmp_path):
        client = FakeSlack(messages=[{
            "ts": "1712345678.000900", "user": "U1", "text": "here",
            "files": [{"name": "trace.log", "mimetype": "text/plain",
                       "url_private_download": "https://files.slack.com/trace.log"}],
        }])
        monkeypatch.setattr(thread_access, "_slack_client", lambda token: client)

        monkeypatch.setattr(
            thread_access.requests, "get", lambda *a, **kw: _SlackDownload(b"a traceback"),
        )
        payload = read_thread(SLACK_LINK, env=SLACK_ENV, store=store)
        attachment = payload["messages"][0]["attachments"][0]
        assert attachment["path"].startswith("/home/developer/.lmer-uploads/")
        assert attachment["reference"].startswith("[lmer upload] /home/developer/.lmer-uploads/")
        written = list((tmp_path / "uploads").iterdir())
        assert [path.read_bytes() for path in written] == [b"a traceback"]
        assert oct(written[0].stat().st_mode)[-3:] == "600"

    def test_an_oversized_file_is_not_read_into_memory_first(self, monkeypatch, store):
        """The bound has to stop the transfer, not describe it afterwards: the
        file is posted by whoever is in the thread, and on the platform route the
        process fetching it serves the whole fleet."""
        client = FakeSlack(messages=[{
            "ts": "1712345678.000900", "user": "U1", "text": "",
            "files": [{"name": "core.dump", "mimetype": "application/octet-stream",
                       "url_private_download": "https://files.slack.com/core.dump"}],
        }])
        monkeypatch.setattr(thread_access, "_slack_client", lambda token: client)
        monkeypatch.setattr(thread_access, "MAX_FILE_BYTES", 1024)
        download = _SlackDownload(b"x" * (1024 * 64))
        monkeypatch.setattr(thread_access.requests, "get", lambda *a, **kw: download)
        payload = read_thread(SLACK_LINK, env=SLACK_ENV, store=store)
        assert download.read <= 1024 + 65536  # one chunk past the bound, at most
        line = render_record(payload["messages"][0])
        assert "over the size this copies in" in line or "could not be fetched" in line

    def test_a_declared_length_over_the_bound_is_not_fetched_at_all(self, monkeypatch, store):
        client = FakeSlack(messages=[{
            "ts": "1712345678.000900", "user": "U1", "text": "",
            "files": [{"name": "core.dump", "url_private_download": "https://files.slack.com/x"}],
        }])
        monkeypatch.setattr(thread_access, "_slack_client", lambda token: client)
        monkeypatch.setattr(thread_access, "MAX_FILE_BYTES", 1024)
        download = _SlackDownload(b"x" * 4096, declared=4096)
        monkeypatch.setattr(thread_access.requests, "get", lambda *a, **kw: download)
        read_thread(SLACK_LINK, env=SLACK_ENV, store=store)
        assert download.read == 0

    def test_a_file_that_failed_to_download_does_not_blame_the_upload_store(
        self, monkeypatch, store
    ):
        """The session *has* a store; saying it does not sends the agent after
        the one thing that would not help."""
        client = FakeSlack(messages=[{
            "ts": "1712345678.000900", "user": "U1", "text": "",
            "files": [{"name": "build.log", "url_private_download": "https://files.slack.com/x"}],
        }])
        monkeypatch.setattr(thread_access, "_slack_client", lambda token: client)
        monkeypatch.setattr(thread_access, "fetch_slack_file", lambda *a, **kw: None)
        payload = read_thread(SLACK_LINK, env=SLACK_ENV, store=store)
        line = render_record(payload["messages"][0])
        assert "no upload store" not in line
        assert "could not be fetched" in line

    def test_a_read_stops_copying_once_its_whole_budget_is_spent(self, monkeypatch, store):
        """`--limit 200` over a file-heavy thread would otherwise stage 200 times
        the per-file bound."""
        client = FakeSlack(messages=[
            {"ts": f"1712345678.00090{index}", "user": "U1", "text": "",
             "files": [{"name": f"f{index}.bin", "url_private_download": "https://files.slack.com/x"}]}
            for index in range(4)
        ])
        monkeypatch.setattr(thread_access, "_slack_client", lambda token: client)
        monkeypatch.setattr(thread_access, "MAX_READ_BYTES", 2048)
        monkeypatch.setattr(
            thread_access, "fetch_slack_file", lambda *a, **kw: b"y" * 1024,
        )
        payload = read_thread(SLACK_LINK, env=SLACK_ENV, store=store)
        copied = [
            record for message in payload["messages"]
            for record in message["attachments"] if record.get("reference")
        ]
        assert len(copied) == 2
        assert "as much as it will" in render_record(payload["messages"][3])

    def test_without_a_store_a_file_is_still_described(self, monkeypatch):
        client = FakeSlack(messages=[{
            "ts": "1712345678.000900", "user": "U1", "text": "",
            "files": [{"name": "trace.log", "mimetype": "text/plain", "size": 11,
                       "url_private_download": "https://files.slack.com/trace.log"}],
        }])
        monkeypatch.setattr(thread_access, "_slack_client", lambda token: client)
        payload = read_thread(SLACK_LINK, env=SLACK_ENV, store=None)
        line = render_record(payload["messages"][0])
        assert "trace.log" in line and "no upload store" in line

    @pytest.mark.parametrize("path", [
        "/etc/shadow",
        "/home/developer/.lmer-uploads/../../etc/shadow",
        "/home/developer/.lmer-uploads/",
        "/home/developer/.lmer-uploads/sub/notes.md",
    ])
    def test_an_attachment_is_resolved_inside_the_store_or_nowhere(self, store, path):
        assert stored_name_of({"path": path}, store) is None

    def test_an_attachment_the_session_staged_resolves_by_name(self, store, tmp_path):
        (tmp_path / "uploads" / "notes.md").write_text("hi")
        assert read_from_store(
            {"path": "/home/developer/.lmer-uploads/notes.md"}, store
        ) == b"hi"

    def test_a_symlink_planted_in_the_store_reads_nothing(self, store, tmp_path):
        """The store is bind-mounted rw into the container, so the name the
        session sends can point anywhere by the time the host opens it. Refused on
        the descriptor (O_NOFOLLOW), not on the name."""
        secret = tmp_path / "host-secret"
        secret.write_text("the daemon's own state")
        (tmp_path / "uploads" / "notes.md").symlink_to(secret)
        with pytest.raises(ThreadError) as caught:
            read_from_store({"path": "/home/developer/.lmer-uploads/notes.md"}, store)
        assert "upload store" in str(caught.value)

    def test_a_directory_or_a_fifo_at_that_name_is_refused(self, store, tmp_path):
        (tmp_path / "uploads" / "notes.md").mkdir()
        with pytest.raises(ThreadError):
            read_from_store({"path": "/home/developer/.lmer-uploads/notes.md"}, store)

    def test_a_staged_file_is_written_through_a_descriptor_not_a_name(self, store, tmp_path):
        """A symlink planted at the name a read is about to write must not become
        a write to its target — nor a chmod of it."""
        from lmer_cli.thread_access import stage_upload

        victim = tmp_path / "victim"
        record = stage_upload(store, "notes.md", b"bytes", "text/plain")
        assert record is not None
        written = tmp_path / "uploads" / Path(record["path"]).name
        # The same name again, now a symlink to somewhere else: O_EXCL refuses
        # the create rather than following it, and the next name is taken.
        written.unlink()
        written.symlink_to(victim)
        again = stage_upload(store, "notes.md", b"other bytes", "text/plain")
        assert victim.exists() is False
        assert again is None or Path(again["path"]).name != written.name


class TestPosting:
    def test_a_post_is_attributed_to_the_run_and_names_its_files(self, slack):
        result = post_thread(
            SLACK_LINK, "the review is posted", label="unified comms (agents/global/…)",
            env=SLACK_ENV, attachments=[{"name": "review.md"}],
        )
        _channel, _ts, text = slack.posted[0]
        assert text.startswith("[unified comms (agents/global/…)] the review is posted")
        assert "📎 review.md" in text
        assert result["posted"]["ref"].endswith(":1712345679.000100")

    def test_an_empty_post_is_refused_before_any_call(self, slack):
        with pytest.raises(ThreadError):
            post_thread(SLACK_LINK, "   ", label="run", env=SLACK_ENV)
        assert slack.posted == []

    def test_matrix_withholds_an_attachment_until_media_is_authenticated(self, matrix, store, tmp_path):
        (tmp_path / "uploads" / "notes.md").write_text("hi")
        item = {"name": "notes.md", "path": "/home/developer/.lmer-uploads/notes.md",
                "media_type": "text/plain"}
        post_thread(MATRIX_LINK, "here it is", label="run", env=MATRIX_ENV,
                    attachments=[item], store=store)
        assert matrix.uploaded == []
        assert any("withheld" in text and msgtype == "m.notice" for text, msgtype in matrix.sent)

    def test_matrix_uploads_the_bytes_under_the_operators_assertion(self, matrix, store, tmp_path):
        (tmp_path / "uploads" / "notes.md").write_text("hi")
        item = {"name": "notes.md", "path": "/home/developer/.lmer-uploads/notes.md",
                "media_type": "text/plain"}
        post_thread(
            MATRIX_LINK, "here it is", label="run",
            env={**MATRIX_ENV, "LMER_MATRIX_AUTHENTICATED_MEDIA": "1"},
            attachments=[item], store=store,
        )
        assert [name for _mxc, name, _size in matrix.uploaded] == ["notes.md"]


class TestWhoPostedIt:
    """The machine-readable half of a post's attribution (#427)."""

    POSTER = thread_posts.Poster.for_session("s-other", "chat-mr-288")

    def test_a_slack_post_records_which_run_made_it(self, slack):
        result = post_thread(
            SLACK_LINK, "have a look", label="chat-mr-288", env=SLACK_ENV, poster=self.POSTER,
        )
        ref = result["posted"]["ref"]
        assert thread_posts.lookup("C0123ABCDEF", "1712345679.000100",
        ) == self.POSTER

    def test_an_unattributed_slack_post_records_nothing(self, slack):
        result = post_thread(SLACK_LINK, "have a look", label="x", env=SLACK_ENV)
        assert thread_posts.lookup("C0123ABCDEF", "1712345679.000100",
        ) is None

    def test_a_matrix_post_carries_it_on_the_event(self, matrix):
        post_thread(MATRIX_LINK, "have a look", label="chat-mr-288",
                    env=MATRIX_ENV, poster=self.POSTER)
        assert thread_posts.poster_from_matrix(matrix.stamps[0]) == self.POSTER

    def test_an_unattributed_matrix_post_carries_nothing(self, matrix):
        post_thread(MATRIX_LINK, "have a look", label="x", env=MATRIX_ENV)
        assert matrix.stamps == [{}]

    def test_an_uploaded_file_event_is_attributed_too(self, matrix, store, tmp_path):
        """The file event is a second app-authored event; unstamped, the
        receiving run is told about a post whose file it never sees, and the
        file is the half it cannot reconstruct (review of !300)."""
        (tmp_path / "uploads" / "notes.md").write_text("hi")
        post_thread(
            MATRIX_LINK, "here it is", label="chat-mr-288", store=store,
            env={**MATRIX_ENV, "LMER_MATRIX_AUTHENTICATED_MEDIA": "1"},
            attachments=[{"name": "notes.md", "path": "/home/developer/.lmer-uploads/notes.md",
                          "media_type": "text/plain"}],
            poster=self.POSTER,
        )
        assert [name for _mxc, name, _size in matrix.uploaded] == ["notes.md"]
        assert all(
            thread_posts.poster_from_matrix(stamp) == self.POSTER
            for stamp in matrix.stamps
        ), "every event the post made carries the stamp, the file's included"

    def test_the_withheld_note_is_attributed_too(self, matrix, store, tmp_path):
        """It is the same post, and a note nothing can attribute is a note the
        receiving run never sees."""
        (tmp_path / "uploads" / "notes.md").write_text("hi")
        post_thread(
            MATRIX_LINK, "here it is", label="chat-mr-288", env=MATRIX_ENV, store=store,
            attachments=[{"name": "notes.md", "path": "/home/developer/.lmer-uploads/notes.md",
                          "media_type": "text/plain"}],
            poster=self.POSTER,
        )
        assert any("withheld" in text and msgtype == "m.notice" for text, msgtype in matrix.sent)
        assert all(
            thread_posts.poster_from_matrix(stamp) == self.POSTER for stamp in matrix.stamps
        )
class TestResolvingBeforeADecision:
    """`slack_thread`'s two answers, told apart (!299 review, round 2).

    A caller that is about to *decide* something — a live connect — must read
    `not_in_channel` as Slack refusing, while a caller merely resolving an
    identity must not fail over a transport that could not be asked."""

    def _client(self, code):
        class Failing(FakeSlack):
            def get_replies(inner, channel, ts, oldest=None):
                from slack_chat.client import SlackError

                raise SlackError(
                    f"Slack API error: {code}" if code
                    else "Slack API rate limited after 3 retries (HTTP 429)",
                    code=code,
                )

        return Failing()

    @pytest.mark.parametrize("code", [
        "not_in_channel", "channel_not_found",
        # What `conversations.replies` documents for the same fact — "make sure
        # your app is a member of the conversation" — where `not_in_channel` is
        # `chat.postMessage`'s spelling of it. Both, because neither the docs nor
        # a reviewer can settle which one a real workspace answers with here, and
        # the code that never fires costs nothing (!299 round 3).
        "no_permission", "access_denied",
    ])
    def test_a_code_slack_states_is_a_refusal_when_one_was_asked_for(self, code):
        with pytest.raises(ThreadError) as caught:
            thread_access.slack_rooted(
                parse_thread(SLACK_LINK), self._client(code), refuse_refusals=True,
            )
        assert code in str(caught.value)

    def test_a_transport_that_could_not_answer_is_still_best_effort(self):
        """`code is None`: the only shape a transport failure has — the client
        raises the exhausted 429 and every request exception without one."""
        ref = parse_thread(SLACK_LINK)
        assert thread_access.slack_rooted(
            ref, self._client(None), refuse_refusals=True,
        ) == ref

    def test_a_code_outside_the_set_is_not_a_refusal(self):
        """An answer Slack gave that does not say the conversation is off
        limits. `ratelimited` is the live example, and this is the test that
        says it must stay tolerated if the classifier is ever widened."""
        ref = parse_thread(SLACK_LINK)
        assert thread_access.slack_rooted(
            ref, self._client("ratelimited"), refuse_refusals=True,
        ) == ref

    def test_a_transient_code_given_a_hint_does_not_become_a_refusal(self):
        """Round 4's minor: the set used to be derived from the hint table, so
        adding hint text for `service_unavailable` / `internal_error` /
        `fatal_error` — documented, transient, and the natural thing to do when
        an operator reports a confusing failure — would silently have made each
        one refuse, and a refusal after `open` posted the root is an orphaned
        root message. Membership is stated in one place now, and this is the
        test that says the two are not coupled."""
        from lmer_cli import thread_access as ta

        hinted = dict(ta._SLACK_HINTS, service_unavailable="Slack is having a bad day")
        with mock.patch.object(ta, "_SLACK_HINTS", hinted):
            ref = parse_thread(SLACK_LINK)
            assert ta.slack_rooted(
                ref, self._client("service_unavailable"), refuse_refusals=True,
            ) == ref

    def test_a_read_scope_gap_is_not_a_refusal(self):
        """`missing_scope` is the one exclusion, and it is the whole of round
        3's second major: a token can be a member of a thread it may not read
        (`groups:history` is optional in the documented set), and refusing here
        after `open` posted a root message loses the record an operator would
        disconnect. It stays a visible runtime failure with a record attached.
        """
        ref = parse_thread(SLACK_LINK)
        assert thread_access.slack_rooted(
            ref, self._client("missing_scope"), refuse_refusals=True,
        ) == ref

    def test_resolution_alone_refuses_nothing(self):
        """The default, which is what a recorded launch target and a one-shot
        read or post go through."""
        ref = parse_thread(SLACK_LINK)
        assert thread_access.slack_rooted(ref, self._client("not_in_channel")) == ref


class TestNamingAChannel:
    """One field, two transports (issue #429). A matrix room carries a
    ``:server`` half and a Slack channel does not, which is the whole rule."""

    @pytest.mark.parametrize("spec,channel", [
        ("#a-channel", "slack"),
        ("C0123ABCDEF", "slack"),
        ("!room:example.org", "matrix"),
        ("#lmer:example.org", "matrix"),
    ])
    def test_each_spelling_reaches_its_transport(self, spec, channel):
        assert parse_channel(spec).channel == channel

    @pytest.mark.parametrize("spec", ["", "   ", None, 7, "two words", "a/b", "x" * 300])
    def test_what_is_not_a_channel_is_refused(self, spec):
        with pytest.raises(ThreadError):
            parse_channel(spec)

    def test_a_thread_permalink_is_told_what_to_do_with_it(self):
        with pytest.raises(ThreadError) as caught:
            parse_channel(SLACK_LINK)
        assert "connect that thread" in str(caught.value)


class TestSlackEgressRedaction:
    """The thread-post paths scrub like their Matrix twins (#539); the bus keeps the raw text."""

    SHAPED = "ghp_" + "Zy9Xw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0Fe9Dc8"

    def test_a_post_reaches_slack_redacted_and_the_attachment_names_too(self, slack):
        post_thread(
            SLACK_LINK, f"found {self.SHAPED}", label="run", env=SLACK_ENV,
            attachments=[{"name": f"{self.SHAPED}.md"}],
        )
        assert self.SHAPED not in slack.posted[0][2]
        assert "***REDACTED***" in slack.posted[0][2]

    def test_a_credential_cut_by_the_clip_is_still_redacted(self, slack):
        lead = "x " * ((thread_access.MAX_POST_CHARS - 12) // 2)
        post_thread(SLACK_LINK, lead + self.SHAPED, label="r", env=SLACK_ENV)
        assert "ghp_" not in slack.posted[0][2]

    def test_an_opened_root_reaches_slack_redacted(self, slack):
        open_thread("#a-channel", f"the run {self.SHAPED}", env=SLACK_ENV)
        assert self.SHAPED not in slack.opened[0][1]
        assert "***REDACTED***" in slack.opened[0][1]


class TestOpening:
    """Opening a thread in a named channel (issue #429).

    The refusals matter more than the happy path here: this is the one call in
    the module that *creates* something in a channel a person named by hand, and
    a root message cannot be unposted.
    """

    def test_a_slack_thread_is_opened_and_named_by_its_permalink(self, slack):
        result = open_thread("#a-channel", "the unified comms run", env=SLACK_ENV)
        assert slack.opened == [("#a-channel", "the unified comms run")]
        assert result["thread"]["permalink"] == (
            "https://example.slack.com/archives/C0123ABCDEF/p1712345678000900"
        )
        assert result["thread"]["channel"] == "slack"

    def test_the_root_carries_no_label_prefix(self, slack):
        """Unlike a post: the root's own text names the run, so a prefix would
        repeat the line under it."""
        open_thread("#a-channel", "the run\nhost/project/slug", env=SLACK_ENV)
        assert slack.opened[0][1].startswith("the run")

    def test_a_channel_the_bot_is_not_in_is_refused_by_name(self, monkeypatch):
        client = FakeSlack(fail="Slack API error: not_in_channel", code="not_in_channel")
        monkeypatch.setattr(thread_access, "_slack_client", lambda token: client)
        with pytest.raises(ThreadError) as caught:
            open_thread("#a-channel", "hello", env=SLACK_ENV)
        assert "not_in_channel" in str(caught.value)
        assert "invite it" in str(caught.value)

    def test_a_matrix_room_is_opened_as_a_plain_event(self, matrix):
        result = open_thread("!room:example.org", "the run", env=MATRIX_ENV)
        assert matrix.sent == [("the run", "m.text")]
        assert result["thread"]["permalink"].endswith("%24root")

    def test_a_room_alias_is_resolved_before_the_post(self, matrix):
        result = open_thread("#lmer:example.org", "the run", env=MATRIX_ENV)
        assert "%21resolved%3Aexample.org" in result["thread"]["permalink"]

    def test_a_room_the_bot_is_not_in_is_refused_before_the_post(self, monkeypatch):
        """And by name: membership is asked before the encryption probe, which
        is a room-state read a non-member cannot make (review finding on !299)."""
        client = FakeMatrix(joined=[])
        monkeypatch.setattr(thread_access, "_matrix_client", lambda account: client)
        with pytest.raises(ThreadError) as caught:
            open_thread("!room:example.org", "the run", env=MATRIX_ENV)
        assert "!room:example.org" in str(caught.value)
        assert "invite it" in str(caught.value)
        assert client.sent == []

    def test_an_encrypted_room_is_refused_before_the_post(self, monkeypatch):
        client = FakeMatrix(encrypted=True)
        monkeypatch.setattr(thread_access, "_matrix_client", lambda account: client)
        with pytest.raises(ThreadError) as caught:
            open_thread("!room:example.org", "the run", env=MATRIX_ENV)
        assert "end-to-end encrypted" in str(caught.value)
        assert client.sent == []

    def test_a_permalink_is_not_a_channel(self, slack):
        with pytest.raises(ThreadError) as caught:
            open_thread(SLACK_LINK, "the run", env=SLACK_ENV)
        assert "connect that thread" in str(caught.value)
        assert slack.opened == []

    def test_a_root_with_no_text_is_refused_before_any_call(self, slack):
        with pytest.raises(ThreadError):
            open_thread("#a-channel", "   ", env=SLACK_ENV)
        assert slack.opened == []

    def test_an_app_that_can_post_uninvited_is_refused_by_name(self, monkeypatch):
        """The one case lmer cannot enforce #429's rule in: with
        `chat:write.public` the app posts into any public channel, so
        `not_in_channel` — the whole of the Slack membership check — never
        fires. Operator decision on !299: refuse, rather than post into a
        channel nobody invited the bot to."""
        client = FakeSlack(scopes=["chat:write", "chat:write.public"])
        monkeypatch.setattr(thread_access, "_slack_client", lambda token: client)

        with pytest.raises(ThreadError) as caught:
            open_thread("#a-channel", "the run", env=SLACK_ENV)

        assert caught.value.status == 403
        assert "chat:write.public" in str(caught.value)
        assert "#a-channel" in str(caught.value)
        assert client.opened == []

    def test_the_documented_scope_set_opens_normally(self, monkeypatch):
        """The other branch of the same header: membership stays Slack's
        `not_in_channel` to enforce, which is what the documented scopes give."""
        client = FakeSlack(scopes=["chat:write", "channels:history", "groups:history"])
        monkeypatch.setattr(thread_access, "_slack_client", lambda token: client)
        open_thread("#a-channel", "the run", env=SLACK_ENV)
        assert client.opened[0][0] == "#a-channel"

    def test_a_header_that_was_not_there_is_not_evidence_of_the_scope(self, monkeypatch):
        """A proxy that strips `x-oauth-scopes` must not make every open fail:
        not knowing is not the same answer as knowing the scope is granted."""
        client = FakeSlack()
        client.scopes = None
        monkeypatch.setattr(thread_access, "_slack_client", lambda token: client)
        open_thread("#a-channel", "the run", env=SLACK_ENV)
        assert client.opened[0][0] == "#a-channel"

    def test_a_root_carries_the_poster_that_opened_it(self, slack, matrix):
        """#427's stamp on #429's root message. Without it the root is
        unattributable, which every connector on the thread — the attaching run's
        included — reads as an own-echo to skip."""
        poster = thread_posts.Poster.for_session("s-1", "develop-429")

        open_thread("#a-channel", "the run", env=SLACK_ENV, poster=poster)
        assert thread_posts.lookup("C0123ABCDEF", "1712345678.000900") == poster

        open_thread("!room:example.org", "the run", env=MATRIX_ENV, poster=poster)
        assert thread_posts.poster_from_matrix(matrix.stamps[0]) == poster

    def test_an_unattributed_root_stamps_nothing(self, slack, matrix):
        """The shape a caller with no session behind it produces, and the one
        `post_thread` already had: nothing recorded, no content key."""
        open_thread("#a-channel", "the run", env=SLACK_ENV)
        assert thread_posts.lookup("C0123ABCDEF", "1712345678.000900") is None

        open_thread("!room:example.org", "the run", env=MATRIX_ENV)
        assert matrix.stamps == [{}]

    def test_a_missing_credential_names_the_environment_it_belongs_to(self):
        with pytest.raises(ThreadError) as caught:
            open_thread("#a-channel", "the run", env={}, where=CLI_ENVIRONMENT)
        assert "SLACK_BOT_TOKEN" in str(caught.value)
        assert CLI_ENVIRONMENT in str(caught.value)


class TestServicer:
    def _servicer(self, **kw):
        return ThreadServicer(supervisor_url="http://127.0.0.1:1", token="t", **kw)

    def test_it_dispatches_a_read_and_a_post(self, slack):
        servicer = self._servicer(label="the run", env=SLACK_ENV)
        read = servicer.handle({"op": "read", "permalink": SLACK_LINK, "limit": 1})
        assert read["ok"] is True and len(read["messages"]) == 1
        posted = servicer.handle({"op": "post", "permalink": SLACK_LINK, "text": "done"})
        assert posted["ok"] is True
        assert slack.posted[0][2].startswith("[the run] done")

    def test_the_label_is_read_per_post_so_a_run_can_be_named_later(self, slack):
        title = {"value": ""}
        servicer = self._servicer(label=lambda: title["value"] or "untitled run", env=SLACK_ENV)
        servicer.handle({"op": "post", "permalink": SLACK_LINK, "text": "one"})
        title["value"] = "the unified comms run"
        servicer.handle({"op": "post", "permalink": SLACK_LINK, "text": "two"})
        assert slack.posted[0][2].startswith("[untitled run]")
        assert slack.posted[1][2].startswith("[the unified comms run]")

    def test_a_post_says_which_session_and_which_run_made_it(self, slack, monkeypatch):
        servicer = self._servicer(
            label="lmer session s-mine", run_name="chat-mr-288", env=SLACK_ENV,
        )
        monkeypatch.setattr(type(servicer), "resolved_session", lambda _self: "s-mine")
        result = servicer.handle({"op": "post", "permalink": SLACK_LINK, "text": "done"})
        ts = result["posted"]["ref"].rsplit(":", 1)[1]
        recorded = thread_posts.lookup("C0123ABCDEF", ts)
        assert recorded == thread_posts.Poster.for_session("s-mine", "chat-mr-288")
        assert recorded.author == "lmer chat-mr-288", (
            "the run's name, not the [label] the thread shows — that already says lmer"
        )

    def test_a_run_with_no_name_yet_is_stamped_by_its_session_alone(self, slack, monkeypatch):
        servicer = self._servicer(label="an lmer session", env=SLACK_ENV)
        monkeypatch.setattr(type(servicer), "resolved_session", lambda _self: "s-mine")
        result = servicer.handle({"op": "post", "permalink": SLACK_LINK, "text": "done"})
        ts = result["posted"]["ref"].rsplit(":", 1)[1]
        assert thread_posts.lookup("C0123ABCDEF", ts).author == "lmer session s-mine"

    def test_it_asks_the_supervisor_which_session_it_serves_once(self):
        servicer = self._servicer(env=SLACK_ENV)
        asked = []

        class Health:
            def health(_self):
                asked.append(1)
                return {"session": "s-mine"}

        servicer._client = Health()
        assert servicer.resolved_session() == "s-mine"
        assert servicer.resolved_session() == "s-mine"
        assert len(asked) == 1

    def test_a_supervisor_that_will_not_say_costs_the_attribution_not_the_post(self, slack):
        """The post lands and carries NO stamp — not a session-origin stamp
        missing its session id, which every connector including this run's own
        would read as another run's (review of !300)."""
        servicer = self._servicer(label="chat-mr-288", run_name="chat-mr-288", env=SLACK_ENV)

        class Mute:
            def health(_self):
                raise bus.BusError("no")

        servicer._client = Mute()
        assert servicer.resolved_session() is None
        assert servicer.poster() is None
        result = servicer.handle({"op": "post", "permalink": SLACK_LINK, "text": "done"})
        assert result["ok"] is True
        ts = result["posted"]["ref"].rsplit(":", 1)[1]
        assert thread_posts.lookup("C0123ABCDEF", ts) is None

    def test_an_unknown_operation_is_refused(self, slack):
        with pytest.raises(ThreadError):
            self._servicer(env=SLACK_ENV).handle({"op": "delete", "permalink": SLACK_LINK})

    def test_a_runs_own_thread_verbs_need_a_host_side_that_keeps_connections(self, slack):
        """The shell ``lmer`` process takes its threads as launch arguments, so
        the refusal has to say that rather than look like a broken channel."""
        servicer = self._servicer(env=SLACK_ENV)
        for request in (
            {"op": "open", "channel": "#a-channel"},
            {"op": "connect", "permalink": SLACK_LINK},
            {"op": "disconnect", "permalink": SLACK_LINK},
        ):
            with pytest.raises(ThreadError) as caught:
                servicer.handle(request)
            assert "launch arguments" in str(caught.value)

    def test_the_three_are_handed_to_the_connections_object(self, slack):
        seen = []

        class _Connections:
            def open(self, channel, *, text="", poster=None):
                seen.append(("open", channel, text, poster))
                return {"thread": {"permalink": SLACK_LINK}}

            def connect(self, permalink):
                seen.append(("connect", permalink))
                return {"thread": {"permalink": permalink}}

            def disconnect(self, permalink):
                seen.append(("disconnect", permalink))
                return {"thread": {"permalink": permalink}}

        servicer = self._servicer(env=SLACK_ENV, connections=_Connections())
        opened = servicer.handle({"op": "open", "channel": "#a-channel", "text": "the run"})
        servicer.handle({"op": "connect", "permalink": SLACK_LINK})
        servicer.handle({"op": "disconnect", "permalink": SLACK_LINK})
        assert opened["ok"] is True and opened["thread"]["permalink"] == SLACK_LINK
        # The open carries the poster this side composed (#427); a servicer that
        # cannot name its session hands over `None` rather than half a stamp.
        assert seen == [
            ("open", "#a-channel", "the run", None),
            ("connect", SLACK_LINK),
            ("disconnect", SLACK_LINK),
        ]

    def test_a_failed_request_becomes_a_result_the_asker_can_read(self, monkeypatch):
        client = FakeSlack(fail="Slack API error: not_in_channel", code="not_in_channel")
        monkeypatch.setattr(thread_access, "_slack_client", lambda token: client)
        servicer = self._servicer(env=SLACK_ENV)
        answered = []
        servicer._client = type("C", (), {"thread_complete": lambda _self, rid, result: answered.append(result)})()
        servicer._answer({"id": "r1", "op": "read", "permalink": SLACK_LINK})
        assert answered[0]["ok"] is False
        assert "invite it" in answered[0]["error"]
