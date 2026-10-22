"""The agent's attachments reach a Slack thread as files (#459, #580).

Both doors — the connector mirroring a run's bus, and ``post --thread`` — run
the same rules as matrix (:func:`lmer_cli.thread_access.outbound_file`): bytes
from the session's store, a 25 MiB cap, the credential scrub, and a
``📎 <name> withheld: <reason>`` note in place of a file that is not sent.
"""
from __future__ import annotations

import errno
import json
from types import SimpleNamespace

import pytest

from lmer_cli import connector_lifecycle, thread_access, thread_posts
from lmer_cli.connector_delivery import RecoveryRequired
from lmer_cli.connector_recovery import Recovery
from lmer_cli.thread_access import (
    MAX_FILE_BYTES, UploadStore, outbound_file, post_thread, slack_upload_refusal,
)
from slack_chat.client import SlackClient, SlackError
from slack_chat.connector import _share_refused
from tests.test_slack_connector import (  # noqa: F401 — autouse fixtures
    _SHAPED, FakeSlack, Rig, _clean_lmer_env, registry_dir,
)

CONTAINER = "/home/developer/.lmer-uploads"
THREAD = "1699999999.000000"


class UploadingSlack(FakeSlack):
    """:class:`FakeSlack` with Slack's upload steps; a share shows up in the
    thread as a bot message carrying the file, like the real thing."""

    def __init__(self):
        super().__init__()
        self.uploads: list = []
        self.shared: list = []
        self.upload_error = None

    def upload_external(self, data, *, filename):
        if self.upload_error is not None:
            raise self.upload_error
        file_id = f"F{len(self.uploads) + 1:04d}"
        self.uploads.append((file_id, filename, bytes(data)))
        return file_id

    def complete_upload(self, file_id, channel, thread_ts, *, title):
        self.shared.append((file_id, channel, thread_ts, title))
        self.replies.append({
            "ts": f"{1700002000 + len(self.shared)}.000400", "user": self.bot_user_id,
            "bot_id": "B0LMER", "text": "", "files": [{"id": file_id, "name": title}],
        })
        return file_id


@pytest.fixture
def store(tmp_path):
    directory = tmp_path / "uploads"
    directory.mkdir()
    return directory


@pytest.fixture
def rig(tmp_path, store):
    rig = Rig(tmp_path, uploads=str(store))
    rig.slack = UploadingSlack()
    rig.connector._slack = rig.slack
    rig.connector._ensure_ready()
    yield rig
    rig.connector.stop()
    rig.bus._session._http.close()
    rig.log.close()


def _item(name, media_type="text/csv", stored=None):
    return {"name": name, "media_type": media_type, "bytes": 1,
            "path": f"{CONTAINER}/{stored or name}"}


def _texts(rig):
    return [text for _channel, _ts, text in rig.slack.posted]


class TestConnector:
    def test_a_file_is_shared_into_the_thread_after_its_text(self, rig, store):
        (store / "data.csv").write_bytes(b"a,b\n1,2\n")
        rig.agent("message", "results", attachments=[_item("data.csv")])
        rig.connector._outbound_once()
        assert _texts(rig) == ["results\n📎 data.csv"]
        assert rig.slack.uploads == [("F0001", "data.csv", b"a,b\n1,2\n")]
        assert rig.slack.shared == [("F0001", "C1", THREAD, "data.csv")]

    def test_its_own_share_is_not_read_back_as_a_message(self, rig, store):
        rig.connector._poll_thread(replay=True)
        (store / "data.csv").write_bytes(b"a,b\n")
        rig.agent("message", "results", attachments=[_item("data.csv")])
        rig.connector._outbound_once()
        before = rig.log.seq
        rig.connector._poll_thread()
        assert rig.log.seq == before
        assert rig.connector._posters.snapshot()["held"] == [], "known at once, not held to time out"

    def test_its_share_is_known_after_a_restart_too(self, rig, store):
        """The in-memory set is lost with the process; the host registry, keyed
        by file id, is not."""
        rig.connector._poll_thread(replay=True)
        (store / "data.csv").write_bytes(b"a,b\n")
        rig.agent("message", "results", attachments=[_item("data.csv")])
        rig.connector._outbound_once()
        rig.connector._uploaded.clear()
        rig.connector._poll_thread()
        assert rig.connector._posters.snapshot()["held"] == []

    def test_a_record_posted_into_another_thread_does_not_upload_here(self, rig, store):
        (store / "data.csv").write_bytes(b"a,b\n")
        rig.agent("message", "results", attachments=[_item("data.csv")],
                  meta={"thread": "https://example.slack.com/archives/C9/p1712345678000900"})
        rig.connector._outbound_once()
        assert rig.slack.uploads == [] and len(rig.slack.posted) == 1

    def test_a_refused_share_becomes_a_note(self, rig, store):
        (store / "data.csv").write_bytes(b"a,b\n")
        rig.agent("message", "results", attachments=[_item("data.csv")])

        def refuse(*args, **kwargs):
            raise SlackError("Slack API error: restricted_action", code="restricted_action")

        rig.slack.complete_upload = refuse
        rig.connector._outbound_once()
        assert _texts(rig) == [
            "results\n📎 data.csv",
            "📎 data.csv withheld: Slack refused the upload (restricted_action)",
        ]
        assert rig.connector._state.data["pending"] is None, "the record finished"

    def test_a_share_slack_definitively_refused_becomes_a_note(self, rig, store):
        (store / "data.csv").write_bytes(b"a,b\n")
        rig.agent("message", "results", attachments=[_item("data.csv")])

        def refuse(*args, **kwargs):
            raise SlackError("Slack API error: file_not_found", code="file_not_found")

        rig.slack.complete_upload = refuse
        rig.connector._outbound_once()
        assert _texts(rig) == [
            "results\n📎 data.csv",
            "📎 data.csv withheld: Slack refused the upload (file_not_found)",
        ]
        assert not rig.connector._state.blocked and rig.connector._state.data["pending"] is None

    def test_a_share_that_may_have_landed_freezes_delivery(self, rig, store):
        """Slack documents internal_error as "part of the operation may have
        succeeded"; like an uncertain text send, it waits for recovery."""
        (store / "data.csv").write_bytes(b"a,b\n")
        rig.agent("message", "results", attachments=[_item("data.csv")])

        def fail(*args, **kwargs):
            raise SlackError("Slack API error: internal_error", code="internal_error")

        rig.slack.complete_upload = fail
        with pytest.raises(RecoveryRequired):
            rig.connector._outbound_once()
        assert _texts(rig) == ["results\n📎 data.csv"], "no note for a file that may be shared"
        assert rig.connector._state.blocked

    def test_a_credential_shaped_name_is_scrubbed_everywhere(self, rig, store):
        (store / "f.csv").write_bytes(b"a\n")
        rig.agent("message", "x", attachments=[{**_item("f.csv"), "name": f"{_SHAPED}.csv"}])
        rig.connector._outbound_once()
        ((_id, filename, _data),) = rig.slack.uploads
        assert _SHAPED not in filename and _SHAPED not in rig.slack.shared[0][3]

    def test_text_files_are_scrubbed_before_upload(self, rig, store):
        (store / "env.txt").write_text(f"token={_SHAPED}\n")
        rig.agent("message", "env", attachments=[_item("env.txt", "text/plain")])
        rig.connector._outbound_once()
        ((_id, _name, sent),) = rig.slack.uploads
        assert sent.startswith(b"token=") and _SHAPED.encode() not in sent

    @pytest.mark.parametrize("case, reason", [
        ("credential", thread_access.WITHHELD_CREDENTIAL),
        ("too-large", thread_access.WITHHELD_TOO_LARGE),
        ("unreadable", thread_access.WITHHELD_UNREADABLE),
        ("missing-scope", "the Slack app lacks the files:write scope"),
        ("refused", "Slack refused the upload (invalid_arguments)"),
    ])
    def test_a_file_not_sent_is_said_so_and_the_text_still_posts(self, rig, store, case, reason):
        name = "shot.png"
        if case == "credential":
            (store / name).write_bytes(b"\x89PNG\r\n\x1a\n" + _SHAPED.encode())
        elif case == "too-large":
            with open(store / name, "wb") as handle:
                handle.truncate(MAX_FILE_BYTES + 1)
        elif case != "unreadable":
            (store / name).write_bytes(b"\x89PNG\r\n\x1a\n")
        if case == "missing-scope":
            rig.slack.upload_error = SlackError("Slack API error: missing_scope", code="missing_scope")
        if case == "refused":
            rig.slack.upload_error = SlackError("Slack API error", code="invalid_arguments")
        rig.agent("message", "see", attachments=[_item(name, "image/png")])
        rig.connector._outbound_once()
        assert _texts(rig) == ["see\n📎 shot.png", f"📎 shot.png withheld: {reason}"]
        assert rig.slack.shared == []

    def test_a_transient_failure_retries_the_file_and_not_the_text(self, rig, store):
        (store / "data.csv").write_bytes(b"a,b\n")
        rig.agent("message", "results", attachments=[_item("data.csv")])
        rig.slack.upload_error = SlackError("Request failed: ConnectionError")
        with pytest.raises(SlackError):
            rig.connector._outbound_once()
        assert _texts(rig) == ["results\n📎 data.csv"] and rig.slack.shared == []
        rig.slack.upload_error = None
        rig.connector._outbound_once()
        assert _texts(rig) == ["results\n📎 data.csv"], "the text is not posted twice"
        assert [shared[0] for shared in rig.slack.shared] == ["F0001"]

    def test_an_uploaded_file_is_shared_once_after_a_failed_share(self, rig, store):
        """The upload is recorded before the share, so a retry shares the same
        file instead of uploading a second copy."""
        (store / "data.csv").write_bytes(b"a,b\n")
        rig.agent("message", "results", attachments=[_item("data.csv")])
        complete = rig.slack.complete_upload

        def refuse(*args, **kwargs):
            raise SlackError("Slack API error: ratelimited", code="ratelimited")

        rig.slack.complete_upload = refuse
        with pytest.raises(SlackError):
            rig.connector._outbound_once()
        rig.slack.complete_upload = complete
        rig.connector._outbound_once()
        assert len(rig.slack.uploads) == 1
        assert [shared[0] for shared in rig.slack.shared] == ["F0001"]


class TestSpeakerTag:
    """A withheld note is a post of its own, so it names the session (#574)."""

    @pytest.fixture
    def speaker(self, monkeypatch):
        current = {"label": "run-a"}
        monkeypatch.setattr(connector_lifecycle, "resolve_speaker", lambda session, **kwargs: current["label"])
        return current

    def test_a_file_withheld_before_upload_is_tagged(self, rig, store, speaker):
        rig.agent("message", "see", attachments=[_item("shot.png", "image/png")])
        rig.connector._outbound_once()
        assert _texts(rig) == ["[run-a] see\n📎 shot.png",
                               f"[run-a] 📎 shot.png withheld: {thread_access.WITHHELD_UNREADABLE}"]

    def test_a_refused_share_keeps_the_tag_it_committed_to_across_a_retry(self, rig, store, speaker, monkeypatch):
        """Delivery refuses changed content on a retry, so a run titled between a
        failed note and its retry re-sends the note as first rendered."""
        monkeypatch.setattr(connector_lifecycle, "SPEAKER_REFRESH_SECONDS", 0.0)
        (store / "data.csv").write_bytes(b"a,b\n")
        rig.agent("message", "results", attachments=[_item("data.csv")])
        note_fails = {"value": True}

        def refuse(*args, **kwargs):
            rig.slack.fail_post = note_fails["value"]
            raise SlackError("Slack API error: restricted_action", code="restricted_action")

        rig.slack.complete_upload = refuse
        with pytest.raises(SlackError):
            rig.connector._outbound_once()
        speaker["label"] = "Renamed run"
        note_fails["value"] = rig.slack.fail_post = False
        rig.connector._outbound_once()
        assert _texts(rig) == [
            "[run-a] results\n📎 data.csv",
            "[run-a] 📎 data.csv withheld: Slack refused the upload (restricted_action)",
        ]
        assert rig.connector._state.data["pending"] is None, "the record finished"


class TestPostThread:
    LINK = "https://example.slack.com/archives/C0123ABCDEF/p1712345678000900"

    @pytest.fixture
    def slack(self):
        client = UploadingSlack()
        client.bot_token = "xoxb-test"
        return client

    def _post(self, slack, store, items, poster=None):
        ref = thread_access.parse_thread(self.LINK)
        return post_thread(
            self.LINK, "have a look", label="run", attachments=items,
            store=UploadStore(str(store), CONTAINER), client=slack, ref=ref, poster=poster,
        )

    def test_the_file_is_uploaded_into_the_named_thread(self, slack, store):
        (store / "data.csv").write_bytes(b"a,b\n")
        self._post(slack, store, [_item("data.csv")])
        assert slack.uploads == [("F0001", "data.csv", b"a,b\n")]
        assert slack.shared == [("F0001", "C0123ABCDEF", "1712345678.000900", "data.csv")]

    def test_a_withheld_file_gets_one_attributed_note(self, slack, store):
        (store / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n" + _SHAPED.encode())
        slack.upload_error = SlackError("x", code="missing_scope")
        (store / "b.csv").write_bytes(b"a\n")
        poster = thread_posts.Poster.for_session("s-other", "chat-mr-288")
        self._post(slack, store, [_item("a.png", "image/png"), _item("b.csv")], poster=poster)
        texts = [text for _c, _t, text in slack.posted]
        assert texts[1] == (
            f"[run] 📎 a.png withheld: {thread_access.WITHHELD_CREDENTIAL}\n"
            "📎 b.csv withheld: the Slack app lacks the files:write scope"
        )
        note_ts = f"{1700000000 + 1}.000100"
        assert thread_posts.lookup("C0123ABCDEF", note_ts) == poster

    def test_the_caller_is_told_what_was_withheld(self, slack, store):
        (store / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n" + _SHAPED.encode())
        (store / "b.csv").write_bytes(b"a\n")
        result = self._post(slack, store, [_item("a.png", "image/png"), _item("b.csv")])
        assert result["posted"]["withheld"] == [
            {"name": "a.png", "reason": thread_access.WITHHELD_CREDENTIAL},
        ]

    def test_a_share_is_attributed_by_file_id(self, slack, store):
        """A connector on that thread must relay the share as this run's post,
        not hold it as an unknown app message (§9)."""
        (store / "data.csv").write_bytes(b"a\n")
        poster = thread_posts.Poster.for_session("s-other", "chat-mr-288")
        self._post(slack, store, [_item("data.csv")], poster=poster)
        share = {"ts": "1712345680.000100", "user": "UBOT", "files": [{"id": "F0001"}]}
        assert thread_posts.poster_from_slack("C0123ABCDEF", share) == poster


class TestSharedRules:
    def test_a_file_outside_the_store_is_never_read(self, store, tmp_path):
        (tmp_path / "secret").write_text("x")
        prepared = outbound_file(
            {"name": "s", "path": f"{CONTAINER}/../secret"}, UploadStore(str(store), CONTAINER),
        )
        assert prepared.withheld == thread_access.WITHHELD_UNREADABLE and prepared.data is None

    def test_the_cap_is_the_inbound_cap(self, store):
        with open(store / "big.bin", "wb") as handle:
            handle.truncate(MAX_FILE_BYTES)
        prepared = outbound_file(_item("big.bin", "application/octet-stream"), UploadStore(str(store), CONTAINER))
        assert prepared.withheld is None and len(prepared.data) == MAX_FILE_BYTES == 25 * 1024 * 1024

    @pytest.mark.parametrize("code, expected", [
        (None, None), ("ratelimited", None), ("internal_error", None),
        ("service_unavailable", None), ("fatal_error", None), ("request_timeout", None),
        ("team_added_to_org", None),
        ("missing_scope", "the Slack app lacks the files:write scope"),
        ("not_allowed_token_type", "Slack refused the upload (not_allowed_token_type)"),
        ("Bad <code> /etc", "Slack refused the upload (unknown error)"),
    ])
    def test_which_slack_failures_withhold(self, code, expected):
        assert slack_upload_refusal(SlackError("x", code=code)) == expected

    @pytest.mark.parametrize("code, refused", [
        ("file_not_found", True), ("invalid_channel", True), ("access_denied", True),
        ("restricted_action", True), ("missing_scope", True), ("ratelimited", True),
        ("internal_error", False), ("fatal_error", False), ("service_unavailable", False),
        ("request_timeout", False), ("team_added_to_org", False), ("file_update_failed", False),
        (None, False),
    ])
    def test_which_share_failures_establish_it_was_not_shared(self, code, refused):
        assert _share_refused(SlackError("x", code=code)) is refused

    def test_an_io_error_reading_the_store_withholds_the_file(self, store, monkeypatch):
        (store / "data.csv").write_bytes(b"a\n")

        def eio(descriptor):
            raise OSError(errno.EIO, "Input/output error")

        monkeypatch.setattr(thread_access.os, "fstat", eio)
        prepared = outbound_file(_item("data.csv"), UploadStore(str(store), CONTAINER))
        monkeypatch.undo()
        assert prepared.withheld == thread_access.WITHHELD_UNREADABLE


class TestClientWire:
    """What :class:`SlackClient` sends for the three upload steps."""

    def test_upload_and_share(self, monkeypatch):
        calls = []

        class _Response:
            def __init__(self, body):
                self.body = body
                self.status_code = 200
                self.headers = {}

            def raise_for_status(self):
                pass

            def json(self):
                return self.body

        def api(method, url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith("/files.getUploadURLExternal"):
                return _Response({"ok": True, "upload_url": "https://files.slack.com/upload/v1/abc",
                                  "file_id": "F123"})
            return _Response({"ok": True, "files": [{"id": "F123", "title": "data.csv"}]})

        posted = []
        monkeypatch.setattr(
            "slack_chat.client.requests.post",
            lambda url, **kwargs: (posted.append((url, kwargs)), _Response({}))[1],
        )
        client = SlackClient("xoxb-test")
        client.session.request = api
        assert client.upload_external(b"a,b\n", filename="data.csv") == "F123"
        assert client.complete_upload("F123", "C1", THREAD, title="data.csv") == "F123"

        (get_url, get_kwargs), (complete_url, complete_kwargs) = calls
        assert get_kwargs["data"] == {"filename": "data.csv", "length": 4}
        assert get_kwargs["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
        ((upload_url, upload_kwargs),) = posted
        assert upload_url == "https://files.slack.com/upload/v1/abc" and upload_kwargs["data"] == b"a,b\n"
        assert "Authorization" not in upload_kwargs["headers"], "the token stays off the pre-signed URL"
        assert complete_kwargs["data"]["channel_id"] == "C1"
        assert complete_kwargs["data"]["thread_ts"] == THREAD
        assert json.loads(complete_kwargs["data"]["files"]) == [{"id": "F123", "title": "data.csv"}]

    def test_a_missing_scope_is_a_coded_refusal(self):
        class _Response:
            status_code = 200
            headers = {}

            def raise_for_status(self):
                pass

            def json(self):
                return {"ok": False, "error": "missing_scope", "needed": "files:write"}

        client = SlackClient("xoxb-test")
        client.session.request = lambda method, url, **kwargs: _Response()
        with pytest.raises(SlackError) as caught:
            client.upload_external(b"x", filename="x")
        assert caught.value.code == "missing_scope"
        assert slack_upload_refusal(caught.value) == thread_access.WITHHELD_SLACK_SCOPE



class TestRecoveryReceipt:
    """`connector_recovery receipt` for a Slack file part checks the file id,
    not the (empty) text of whatever message is named."""

    def _verify(self, payload, message):
        recovery = Recovery.__new__(Recovery)
        recovery.key = SimpleNamespace(transport="slack", conversation="C1", root=THREAD)

        class Remote:
            def get_replies(self, channel, root):
                return [message]

            def auth_test(self):
                return "UBOT"

        recovery._verify_remote(message["ts"], payload, Remote(), None)

    def test_the_share_of_this_file_is_accepted(self):
        self._verify({"file_id": "F0001", "name": "a"},
                     {"ts": "1700000001.1", "user": "UBOT", "text": "", "files": [{"id": "F0001"}]})

    def test_another_files_share_is_refused(self):
        with pytest.raises(RecoveryRequired):
            self._verify({"file_id": "F0001", "name": "a"},
                         {"ts": "1700000001.1", "user": "UBOT", "text": "", "files": [{"id": "FOTHER"}]})
