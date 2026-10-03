"""The daemon's end of the unified channel (issue #407, T3).

Runs a **real supervisor bus** — the FastAPI routes of ``lmer_cli.supervisor``
over a real ``MessageLog``, served by uvicorn on loopback — behind a registered
live session, so what is pinned is the whole daemon path: the hub's subscriber
long-polling the control plane and acking, the fleet journal and its cursor,
the source stamped from the caller's credential, the answer-by-bus path with
its 409 for a late answer, the ``/input`` steering conversion, and connector
credentials.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from lmer_cli import comms as bus
from lmer_cli import supervisor
from lmer_platform import api, comms, registry, spawn, store
from lmer_platform import config as cfg
from tests.conftest import strip_lmer_env

SECRET = "test-secret-value"
BUS_TOKEN = "bus-control-plane-token"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture
def config(platform_root):
    return cfg.load()


@pytest.fixture
def client(config):
    return TestClient(api.create_app(config, SECRET))


def auth(secret=SECRET):
    raw = f":{secret}".encode("utf-8")
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


class BusPlane:
    """One session's supervisor, with a bus, on a real loopback port."""

    def __init__(self, tmp_path, session="s-bus-1", *, startup_pending=False, terminal_window=None):
        self.log = bus.MessageLog(session, str(tmp_path / f"{session}.messages.jsonl"))
        self.sink: list[bytes] = []
        buf = supervisor.OutputBuffer(limit=4096)

        def write_input(data: bytes) -> int:
            self.sink.append(bytes(data))
            return len(data)

        self.relay = supervisor._InboundRelay(
            self.log, lambda text: supervisor._submit_payload(write_input, text),
            "claude", startup_pending=True,
        ) if startup_pending else None
        from lmer_cli.terminal_mirror import TerminalMirror, TranscriptReader
        self.mirror = (
            TerminalMirror(self.log, TranscriptReader(None, "unknown"), window=terminal_window)
            if terminal_window is not None else None
        )
        app = supervisor._build_fastapi_app(
            buf, write_input, BUS_TOKEN, comms=self.log,
            inbound_relay=self.relay, terminal_mirror=self.mirror,
        )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self._thread, self._shutdown = supervisor._start_fastapi_server(app, "127.0.0.1", self.port)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.02)

    def stop(self):
        self._shutdown()
        self._thread.join(timeout=3)


@pytest.fixture
def plane(tmp_path, platform_root):
    server = BusPlane(tmp_path)
    yield server
    server.stop()


def live_session(plane, session_id="s-bus-1", **overrides):
    payload = {
        "kind": "worker",
        "pid": os.getpid(),
        "run": {"host": "gitlab.example.com", "project": "agents/global", "slug": "develop-407"},
        "task": {"taskdef": "develop", "target": "issue-407"},
        "control": {"host": "127.0.0.1", "port": plane.port, "token_ref": str(spawn.token_file_for(session_id))},
        "log_path": str(store.logs_dir() / f"{session_id}.log"),
        "started_at": "2026-09-04T09:00:00Z",
    }
    payload.update(overrides)
    registry.register(session_id, **payload)
    spawn.token_file_for(session_id).write_text(BUS_TOKEN, encoding="utf-8")
    log = store.logs_dir() / f"{session_id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(b"")
    return session_id


def agent_posts(plane, kind, text, **kw):
    return plane.log.append(kind=kind, text=text, source=bus.Source("agent"), **kw)[0]


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


class TestJournal:
    def test_cursor_is_dense_and_dedupes_on_message_id(self, tmp_path):
        journal = comms.Journal(tmp_path / "journal.jsonl")
        first = journal.append("s-1", {"id": "s-1/000001", "seq": 1, "kind": "note", "text": "a"})
        assert first.cursor == 1
        assert journal.append("s-1", {"id": "s-1/000001", "seq": 1, "kind": "note", "text": "a"}) is None
        second = journal.append("s-2", {"id": "s-2/000001", "seq": 1, "kind": "note", "text": "b"})
        assert second.cursor == 2 and journal.cursor == 2

    def test_read_since_filters_by_session_and_returns_the_last_cursor(self, tmp_path):
        journal = comms.Journal(tmp_path / "journal.jsonl")
        for index in range(3):
            journal.append("s-1" if index != 1 else "s-2", {"id": f"m/{index}", "seq": index + 1, "kind": "note", "text": "x"})
        events, cursor = journal.read_since(0)
        assert [e.cursor for e in events] == [1, 2, 3] and cursor == 3
        events, cursor = journal.read_since(0, session="s-1")
        assert [e.cursor for e in events] == [1, 3] and cursor == 3
        events, cursor = journal.read_since(3)
        assert events == [] and cursor == 3

    def test_read_since_blocks_for_the_next_event(self, tmp_path):
        journal = comms.Journal(tmp_path / "journal.jsonl")
        result = {}

        def read():
            result["page"] = journal.read_since(0, timeout=5.0)

        thread = threading.Thread(target=read)
        thread.start()
        time.sleep(0.05)
        journal.append("s-1", {"id": "m/1", "seq": 1, "kind": "note", "text": "late"})
        thread.join(5.0)
        events, cursor = result["page"]
        assert len(events) == 1 and cursor == 1

    def test_the_file_reloads_and_old_cursors_are_served_from_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr(comms, "MEMORY_EVENTS", 2)
        path = tmp_path / "journal.jsonl"
        journal = comms.Journal(path)
        for index in range(5):
            journal.append("s-1", {"id": f"m/{index}", "seq": index + 1, "kind": "note", "text": str(index)})
        again = comms.Journal(path)
        assert again.cursor == 5
        events, cursor = again.read_since(0)
        assert [e.cursor for e in events] == [1, 2, 3, 4, 5] and cursor == 5
        assert oct(path.stat().st_mode & 0o777) == "0o600"

    def test_question_fold_over_a_session(self, tmp_path):
        journal = comms.Journal(tmp_path / "journal.jsonl")
        journal.append("s-1", {"id": "s-1/000001", "seq": 1, "kind": "question", "text": "a?"})
        journal.append("s-1", {"id": "s-1/000002", "seq": 2, "kind": "question", "text": "b?"})
        journal.append("s-1", {"id": "s-1/000003", "seq": 3, "kind": "answer", "in_reply_to": "s-1/000001", "text": "x"})
        journal.append("s-1", {"id": "s-1/000004", "seq": 4, "kind": "answer", "in_reply_to": "s-1/000001", "text": "late", "meta": {"late": True}})
        journal.append("s-1", {"id": "s-1/000005", "seq": 5, "kind": "close", "in_reply_to": "s-1/000002", "text": ""})
        states = journal.question_states("s-1")
        assert states["s-1/000001"]["state"] == "answered" and states["s-1/000001"]["answered_by"] == "s-1/000003"
        assert states["s-1/000002"]["state"] == "closed"


# ---------------------------------------------------------------------------
# Connector credentials
# ---------------------------------------------------------------------------


class TestConnectorCredentials:
    def test_mint_list_revoke(self, platform_root):
        credential = cfg.mint_connector_credential("slack-bridge", "slack")
        assert len(credential) > 20
        assert cfg.list_connectors() == [{"name": "slack-bridge", "channel": "slack"}]
        assert cfg.active_connectors()[credential] == {"name": "slack-bridge", "channel": "slack"}
        path = cfg.connector_path("slack-bridge")
        assert oct(path.stat().st_mode & 0o777) == "0o600"
        assert cfg.revoke_connector_credential("slack-bridge") is True
        assert cfg.revoke_connector_credential("slack-bridge") is False
        assert cfg.list_connectors() == []

    def test_names_and_channels_are_validated(self, platform_root):
        with pytest.raises(cfg.ConfigError):
            cfg.mint_connector_credential("Bad Name", "slack")
        with pytest.raises(cfg.ConfigError):
            cfg.mint_connector_credential("ok", "Slack Channel")

    def test_the_daemon_attributes_a_connector_by_its_credential(self, platform_root, client):
        credential = cfg.mint_connector_credential("matrix-side", "matrix")
        resp = client.get("/api/comms/connectors", headers=bearer(credential))
        assert resp.status_code == 200
        assert resp.json()["credentials"] == [{"name": "matrix-side", "channel": "matrix"}]
        assert client.get("/api/comms/connectors", headers=bearer("not-a-credential")).status_code == 401

    def test_the_platform_cli_verbs(self, platform_root, capsys):
        from lmer_platform import daemon

        assert daemon.main(["connector", "add", "slack-bridge", "--channel", "slack"]) == 0
        printed = capsys.readouterr()
        credential = printed.out.strip()
        assert credential and "shown once" in printed.err.lower()
        assert daemon.main(["connector", "list"]) == 0
        assert "slack-bridge\tslack" in capsys.readouterr().out
        assert daemon.main(["connector", "remove", "slack-bridge"]) == 0
        assert daemon.main(["connector", "remove", "slack-bridge"]) == 1


# ---------------------------------------------------------------------------
# Routes over a real bus
# ---------------------------------------------------------------------------


class TestJournalBounds:
    """§5.2's fold is across *live* sessions, and the journal is a cache."""

    def test_the_question_fold_is_kept_rather_than_re_derived(self, tmp_path, platform_root, plane):
        session = live_session(plane)
        hub = comms.CommsHub()
        question = agent_posts(plane, "question", "branch?", options=["main"])
        hub._ingest(session, question.to_dict())
        assert [q["state"] for q in hub.questions(open_only=True)] == ["open"]
        # No full-journal read behind it: the fold answers from memory.
        calls = []
        original = hub.journal._scan_file
        hub.journal._scan_file = lambda *a, **k: calls.append(a) or original(*a, **k)
        hub.questions(open_only=True)
        assert calls == [], "a UI-polled route must not scan the journal file"

    def test_a_dead_sessions_question_leaves_the_fold(self, platform_root, plane):
        session = live_session(plane)
        hub = comms.CommsHub()
        question = agent_posts(plane, "question", "branch?")
        hub._ingest(session, question.to_dict())
        assert len(hub.questions(open_only=True)) == 1
        entry = registry.read_session(session)
        registry.register(session, kind=entry["kind"], pid=2 ** 22, control=entry["control"],
                          log_path=entry["log_path"], started_at=entry["started_at"])
        assert hub.questions(open_only=True) == [], "nobody can answer it now"

    def test_the_journal_rotates_rather_than_growing_without_bound(self, tmp_path, platform_root, plane, monkeypatch):
        monkeypatch.setattr(comms, "MAX_JOURNAL_BYTES", 2048)
        session = live_session(plane)
        hub = comms.CommsHub()
        for index in range(60):
            record = agent_posts(plane, "note", f"line {index} " + "x" * 60)
            hub._ingest(session, record.to_dict())
        rotated = hub.journal.path.with_suffix(hub.journal.path.suffix + ".1")
        assert rotated.exists(), "one previous generation is kept"
        current = hub.journal.path.stat().st_size if hub.journal.path.exists() else 0
        assert current < 2048 * 2
        assert hub.journal.cursor == 60, "the cursor is continuous across a rotation"
        # A reader still gets what is in memory, from any cursor.
        events, _ = hub.feed(0, session=session, limit=100)
        assert events and events[-1].cursor == 60


class TestRoutes:
    def test_a_session_with_no_bus_is_typed_into_rather_than_refused(self, platform_root, client, tmp_path):
        """Spec §8's third row, from the client's side: during the deployment
        window every already-running session has a pre-#407 image, and the two
        callers that used `/input` now post here. A 409 would be the chat
        composer and `lmer-ctl send` failing exactly when they used to work."""
        buf = supervisor.OutputBuffer(limit=1024)
        typed: list = []
        app = supervisor._build_fastapi_app(buf, lambda d: typed.append(bytes(d)) or len(d), BUS_TOKEN)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        thread, shutdown = supervisor._start_fastapi_server(app, "127.0.0.1", port)
        try:
            time.sleep(0.3)

            class Fake:
                pass

            fake = Fake()
            fake.port = port
            session = live_session(fake, session_id="s-old-2")
            resp = client.post(
                f"/api/sessions/{session}/comms",
                json={"kind": "message", "text": "carry on"}, headers=auth(),
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["bus"] is False and body["accepted_as"] == "typed"
            assert b"carry on" in b"".join(typed)
            # A record with nowhere to go still says so rather than being lost.
            refused = client.post(
                f"/api/sessions/{session}/comms",
                json={"kind": "note", "text": "x"}, headers=auth(),
            )
            assert refused.status_code == 409
            # Round 2: an index has nothing to resolve against here, so it is
            # refused BEFORE the typing path rather than after — and the detail
            # names the door that can answer such a question, because retrying
            # here with the option's text would 409 again on the kind.
            before = b"".join(typed)
            no_options = client.post(
                f"/api/sessions/{session}/comms",
                json={"kind": "answer", "option": 2, "in_reply_to": "1"}, headers=auth(),
            )
            assert no_options.status_code == 409
            detail = no_options.json()["detail"]
            assert "option index cannot be resolved here" in detail
            assert f"/api/sessions/{session}/ask/" in detail
            # Nothing at all reached the harness — not a search for "2", which
            # matches the digits inside the bracketed-paste escape itself.
            assert b"".join(typed) == before
            # An index on a kind that cannot carry one is not this branch's
            # business: the message still goes in as a message.
            message = client.post(
                f"/api/sessions/{session}/comms",
                json={"kind": "message", "text": "carry on too", "option": 2}, headers=auth(),
            )
            assert message.status_code == 200 and message.json()["bus"] is False
            assert b"carry on too" in b"".join(typed)
        finally:
            shutdown()
            thread.join(timeout=3)

    def test_every_post_leaves_a_durable_attempt_record(self, platform_root, client, plane):
        """#197's question — "did my message reach the session?" — asked of the
        path the composer, `lmer-ctl send` and the ask answer all take now."""
        from lmer_platform import store as platform_store

        session = live_session(plane)
        resp = client.post(
            f"/api/sessions/{session}/comms",
            json={"kind": "message", "text": "did this land?", "author": "tester"}, headers=auth(),
        )
        assert resp.status_code == 200
        events = [e for e in platform_store.read_events() if e.get("type") == "comms_post"]
        assert len(events) == 1, events
        data = events[0].get("data") or {}
        assert data["session"] == session and data["message_id"] == resp.json()["message"]["id"]
        assert data["receipt_match"] is True and data["accepted_as"] == "new"
        assert "did this land?" not in json.dumps(events[0]), "the text is never written down"

    def test_an_answer_resolved_from_its_index_still_journals_a_matching_receipt(
        self, platform_root, client, plane
    ):
        """#428 review, finding 1: the bus stores the option's text, which is not
        what the caller sent. A receipt over the stored record made this journal
        `receipt_match: false` for a delivery that had in fact succeeded — #194's
        shape, the indicator lying rather than the mechanism failing."""
        from lmer_platform import store as platform_store

        session = live_session(plane)
        question = agent_posts(plane, "question", "which branch?", options=["main", "prep"])
        resp = client.post(
            f"/api/sessions/{session}/comms",
            json={"kind": "answer", "option": 2, "in_reply_to": question.id},
            headers=auth(),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"]["text"] == "prep"
        assert resp.json()["message"]["meta"]["option"] == 2
        events = [e for e in platform_store.read_events() if e.get("type") == "comms_post"]
        assert [(e["data"]["receipt_match"], e["data"]["accepted_as"]) for e in events] \
            == [(True, "new")]

    def test_an_option_index_the_question_does_not_offer_is_a_400(
        self, platform_root, client, plane
    ):
        session = live_session(plane)
        question = agent_posts(plane, "question", "which branch?", options=["main", "prep"])
        resp = client.post(
            f"/api/sessions/{session}/comms",
            json={"kind": "answer", "option": 7, "in_reply_to": question.id},
            headers=auth(),
        )
        assert resp.status_code == 400
        assert "not one of the 2" in resp.json()["detail"]

    def test_the_operator_posts_as_platform_and_the_agent_is_tagged(self, platform_root, client, plane):
        session = live_session(plane)
        resp = client.post(f"/api/sessions/{session}/comms", json={"kind": "message", "text": "!206 was merged", "author": "tester"}, headers=auth())
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["accepted_as"] == "new"
        assert body["message"]["source"] == {"channel": "platform", "author": "tester"}
        assert body["relay"]["tagged"] is True
        assert b"[platform \xc2\xb7 tester] !206 was merged" in b"".join(plane.sink)
        # Journaled at once, visible on the per-session read and the fleet feed.
        page = client.get(f"/api/sessions/{session}/comms", headers=auth()).json()
        assert [m["text"] for m in page["messages"]] == ["!206 was merged"] and page["cursor"] == 1
        feed = client.get("/api/comms/feed", headers=auth()).json()
        assert feed["cursor"] == 1 and feed["events"][0]["session"] == session
        assert feed["events"][0]["run"]["slug"] == "develop-407"

    def test_the_assistant_posts_as_uber_lmer_without_an_author(self, platform_root, client, plane):
        session = live_session(plane)
        minted = cfg.mint_assistant_credential()
        resp = client.post(f"/api/sessions/{session}/comms", json={"kind": "message", "text": "fix the two majors", "author": "ignored"}, headers=bearer(minted))
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"]["source"] == {"channel": "uber-lmer"}
        assert b"[uber lmer] fix the two majors" in b"".join(plane.sink)

    def test_a_connector_posts_as_its_bound_channel_whatever_it_claims(self, platform_root, client, plane):
        session = live_session(plane)
        credential = cfg.mint_connector_credential("slack-bridge", "slack")
        resp = client.post(f"/api/sessions/{session}/comms", json={
            "kind": "message", "text": "hi", "author": "tester", "ref": "1725.1",
            "source": {"channel": "matrix"},
        }, headers=bearer(credential))
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"]["source"] == {"channel": "slack", "author": "tester", "ref": "1725.1"}
        # A retry with the same ref is the same record.
        again = client.post(f"/api/sessions/{session}/comms", json={"kind": "message", "text": "hi", "author": "tester", "ref": "1725.1"}, headers=bearer(credential))
        assert again.json()["accepted_as"] == "duplicate"

    def test_commands_come_from_the_operator_and_the_assistant_only(self, platform_root, client, plane):
        session = live_session(plane)
        credential = cfg.mint_connector_credential("slack-bridge", "slack")
        resp = client.post(f"/api/sessions/{session}/comms", json={"kind": "command", "text": "/followup"}, headers=bearer(credential))
        assert resp.status_code == 403
        resp = client.post(f"/api/sessions/{session}/comms", json={"kind": "command", "text": "/followup"}, headers=auth())
        assert resp.status_code == 200 and resp.json()["relay"]["tagged"] is False

    def test_a_late_answer_on_the_old_route_is_a_409_and_on_the_new_route_is_late(self, platform_root, client, plane):
        session = live_session(plane)
        question = agent_posts(plane, "question", "branch?", options=["main", "prep"])
        first = client.post(f"/api/sessions/{session}/ask/{question.seq:06d}/answer", json={"answer": "prep"}, headers=auth())
        assert first.status_code == 200, first.text
        assert first.json()["question_id"] == f"{question.seq:06d}" and first.json()["message_id"] == f"{session}/000002"
        assert plane.log.question("1").state == bus.STATE_ANSWERED
        second = client.post(f"/api/sessions/{session}/ask/{question.seq:06d}/answer", json={"answer": "main"}, headers=auth())
        assert second.status_code == 409
        third = client.post(f"/api/sessions/{session}/comms", json={"kind": "answer", "text": "main", "in_reply_to": question.id, "author": "alice"}, headers=auth())
        assert third.status_code == 200 and third.json()["accepted_as"] == "late"
        typed = b"".join(plane.sink)
        assert b"(answer to #1 'branch?') prep" in typed and b"late answer to #1" in typed

    def test_the_questions_view_spans_sessions(self, platform_root, client, plane):
        session = live_session(plane)
        agent_posts(plane, "question", "a?")
        # The hub only knows what it journaled: read the session once so the
        # journal has the record (a running daemon's subscriber does this).
        hub = comms.ensure_hub()
        hub.subscribe(session)
        deadline = time.monotonic() + 5
        while hub.journal.cursor < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        resp = client.get("/api/comms/questions?open=true", headers=auth())
        assert [q["question"]["text"] for q in resp.json()["questions"]] == ["a?"]

    def test_input_steering_rides_the_bus_and_says_it_is_deprecated(self, platform_root, client, plane):
        session = live_session(plane)
        resp = client.post(f"/api/sessions/{session}/input", json={"data": "!206 was merged", "append_newline": True, "sanitize": True}, headers=auth())
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["deprecated"] is True and body["message_id"] == f"{session}/000001"
        assert body["submit_confirmed"] is False and "comms" in body["note"]
        # §8.3: the reply keeps its shape — receipt included — and says it is
        # deprecated in its own metadata, not only in the body.
        assert len(body["payload_sha256"]) == 64 and body["payload_length"] == 15
        assert resp.headers["Deprecation"] == "true"
        assert "successor-version" in resp.headers["Link"]
        assert b"[platform \xc2\xb7 operator] !206 was merged" in b"".join(plane.sink)
        # lmer-ctl's old spelling of a slash command becomes a command record.
        resp = client.post(f"/api/sessions/{session}/input", json={"data": "/followup", "append_newline": True, "sanitize": True, "preserve_slash_commands": True}, headers=auth())
        assert resp.status_code == 200
        assert plane.log.get(2).kind == "command"
        # Raw keystrokes are not messages.
        resp = client.post(f"/api/sessions/{session}/input", json={"data": "x"}, headers=auth())
        assert resp.status_code == 200 and "deprecated" not in resp.json()
        assert plane.log.seq == 2

    def test_deprecated_input_reports_the_startup_hold(self, tmp_path, platform_root, client):
        plane = BusPlane(tmp_path, startup_pending=True)
        try:
            session = live_session(plane)
            response = client.post(f"/api/sessions/{session}/input", headers=auth(), json={
                "data": "first direction", "append_newline": True, "sanitize": True,
            })
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["queued"] is True and body["bytes_written"] == 0
            assert "held until startup submission" in body["note"]
            assert plane.log.get(1).text == "first direction" and plane.sink == []
            raw = client.post(f"/api/sessions/{session}/input", headers=auth(), json={"data": "raw"})
            assert raw.status_code == 200 and raw.json()["bytes_written"] == 3
            assert "queued" not in raw.json() and plane.log.seq == 1
            plane.relay.startup_submitted()
            assert b"first direction" in b"".join(plane.sink)
        finally:
            plane.stop()

    def test_an_unknown_session_is_a_404_before_anything_else(self, platform_root, client):
        assert client.post("/api/sessions/s-nope/comms", json={"kind": "message", "text": "x"}, headers=auth()).status_code == 404
        assert client.get("/api/sessions/s-nope/comms", headers=auth()).status_code == 404

    def test_ack_records_a_connector_position(self, platform_root, client):
        credential = cfg.mint_connector_credential("slack-bridge", "slack")
        resp = client.post("/api/comms/ack", json={"cursor": 7}, headers=bearer(credential))
        assert resp.json()["connectors"] == {"slack-bridge": 7}
        resp = client.post("/api/comms/ack", json={"connector": "ui", "cursor": 3}, headers=auth())
        assert resp.json()["connectors"] == {"slack-bridge": 7, "ui": 3}
        assert client.post("/api/comms/ack", json={"cursor": -1}, headers=auth()).status_code == 400


# ---------------------------------------------------------------------------
# The hub's subscriber
# ---------------------------------------------------------------------------


class TestHub:
    def test_terminal_window_updates_a_running_supervisor_through_the_real_transport(
        self, platform_root, tmp_path, monkeypatch, client,
    ):
        monkeypatch.setattr(comms, "POLL_SECONDS", 0.05)
        plane = BusPlane(tmp_path, terminal_window=11)
        hub = comms.CommsHub()
        session = live_session(plane)

        def reaches(value):
            deadline = time.monotonic() + 5
            while plane.mirror.window != value and time.monotonic() < deadline:
                time.sleep(0.02)
            assert plane.mirror.window == value

        try:
            hub.subscribe(session)
            reaches(30)
            reply = client.post("/api/assistant/config",
                                json={"terminal_confirm_seconds": 75}, headers=auth())
            assert reply.status_code == 200
            assert reply.json()["terminal"]["confirm_seconds"]["source"] == "config.json"
            reaches(75)
            monkeypatch.setenv(bus.TERMINAL_CONFIRM_ENV, "90")
            reaches(90)
            effective = client.get("/api/assistant/config", headers=auth()).json()
            assert effective["terminal"]["confirm_seconds"]["source"] == "env"
            monkeypatch.setenv(bus.TERMINAL_CONFIRM_ENV, "invalid")
            reaches(75)
            assert client.post("/api/assistant/config", json={"terminal_confirm_seconds": 0},
                               headers=auth()).status_code == 400
        finally:
            hub.stop()
            plane.stop()

    def test_subscriber_ingests_over_http_acks_and_feeds_consumers(self, platform_root, plane):
        session = live_session(plane)
        hub = comms.CommsHub()
        seen = []
        hub.add_consumer(lambda event: seen.append(event))
        assert hub.supports(session) is True
        hub.subscribe(session)
        agent_posts(plane, "milestone", "pushed MR !300")
        agent_posts(plane, "note", "working")
        deadline = time.monotonic() + 5
        while len(seen) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        hub.stop()
        assert [e.message["kind"] for e in seen] == ["milestone", "note"]
        assert hub.journal.cursor == 2
        assert hub.positions.get(session) == 2
        deadline = time.monotonic() + 3
        while plane.log.subscribers().get("platform") != 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert plane.log.subscribers() == {"platform": 2}
        assert hub.handles(session) is False  # stopped

    def test_supports_is_false_for_a_supervisor_without_a_bus(self, platform_root, tmp_path):
        buf = supervisor.OutputBuffer(limit=1024)
        app = supervisor._build_fastapi_app(buf, lambda d: len(d), BUS_TOKEN)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        thread, shutdown = supervisor._start_fastapi_server(app, "127.0.0.1", port)
        try:
            time.sleep(0.3)

            class Fake:
                pass

            fake = Fake()
            fake.port = port
            session = live_session(fake, session_id="s-old-1")
            assert comms.CommsHub().supports(session) is False
        finally:
            shutdown()
            thread.join(timeout=3)

    def test_digests_are_batched_per_session_and_milestones_are_immediate(self, platform_root, plane, monkeypatch):
        from lmer_platform import assistant, daemon

        session = live_session(plane)
        spooled = []
        monkeypatch.setattr(assistant, "notify", lambda note, kind="event", data=None: spooled.append((kind, note, data)) or True)
        hub = comms.CommsHub()
        hub.add_consumer(daemon._assistant_consumer(hub))
        hub.subscribe(session)
        agent_posts(plane, "milestone", "pushed MR !300")
        plane.log.append(kind="message", text="hi there", source=bus.Source("slack", author="tester", ref="t1"))
        agent_posts(plane, "note", "working on it")
        deadline = time.monotonic() + 5
        while hub.journal.cursor < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
        hub.stop()
        assert [item[0] for item in spooled] == ["session_signal"]
        assert "signalled: pushed MR !300" in spooled[0][1]
        assert hub.flush_digests() == 1
        kind, note, data = spooled[1]
        assert kind == "session_comms" and data["count"] == 2
        assert "[slack · tester] message: hi there" in note and "[agent] note: working on it" in note
        assert "gitlab.example.com/agents/global/develop-407" in note

    def test_a_milestone_the_hub_delivered_is_not_spooled_again_by_the_detector(
        self, platform_root, plane, monkeypatch,
    ):
        """The two readers share one high-water mark. `handles()` is false in
        ordinary situations — before the first reconcile, after a restart — and
        the ask directory still holds the projection of every milestone, so a
        mark left at 0 re-spools a run's whole history."""
        from lmer_platform import assistant, daemon, detect
        from ask_channel import protocol as ask_protocol
        from lmer_platform import ask as ask_channel

        session = live_session(plane)
        monkeypatch.setattr(assistant, "notify", lambda note, kind="event", data=None: True)
        hub = comms.CommsHub()
        hub.add_consumer(daemon._assistant_consumer(hub))
        hub.subscribe(session)
        agent_posts(plane, "milestone", "pushed MR !300")
        # Wait for the *mark*, not the journal cursor: the consumer runs after
        # the append, so a cursor of 1 does not yet mean the milestone was
        # delivered and marked (this raced in CI, pipeline 1993).
        deadline = time.monotonic() + 15
        while not detect._signal_marks().get(session) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert detect._signal_marks().get(session), "the hub never delivered the milestone"
        hub.stop()  # the hub no longer handles the session: a restart, say
        # The supervisor's projection of the same milestone, as an old daemon reads it.
        directory = ask_channel.session_ask_dir(session)
        directory.mkdir(parents=True, exist_ok=True)
        ask_protocol.write_record(directory, "000001" + ask_protocol.SIGNAL_SUFFIX, {
            "schema": ask_protocol.SCHEMA_VERSION, "id": "000001",
            "kind": ask_protocol.KIND_SIGNAL, "text": "pushed MR !300",
            "at": "2026-09-04T00:00:00Z",
        })
        assert detect.new_signals() == [], "already delivered by the hub"

    def test_detect_skips_sessions_the_hub_handles(self, platform_root, plane):
        from lmer_platform import detect

        session = live_session(plane)
        hub = comms.CommsHub()
        comms.register_hub(hub)
        hub.subscribe(session)
        deadline = time.monotonic() + 2
        while not hub.handles(session) and time.monotonic() < deadline:
            time.sleep(0.02)
        # A projection-shaped signal file would otherwise be read here.
        assert detect.new_signals() == []
        hub.stop()


@pytest.mark.parametrize("active_tail", [False, True])
def test_rotated_journal_restarts_with_cursor_fold_and_disk_replay(tmp_path, monkeypatch, active_tail):
    monkeypatch.setattr(comms, "MEMORY_EVENTS", 1)
    monkeypatch.setattr(comms, "MAX_JOURNAL_BYTES", 10 ** 9)
    path = tmp_path / "journal.jsonl"
    journal = comms.Journal(path)
    question = {"id": "s/000001", "seq": 1, "kind": "question", "text": "branch?"}
    journal.append("s", question)
    journal.append("s", {"id": "s/000002", "seq": 2, "kind": "note", "text": "n2"})
    monkeypatch.setattr(comms, "MAX_JOURNAL_BYTES", 1)
    journal.append("s", {"id": "s/000003", "seq": 3, "kind": "note", "text": "n3"})
    monkeypatch.setattr(comms, "MAX_JOURNAL_BYTES", 10 ** 9)
    if active_tail:
        journal.append("s", {"id": "s/000004", "seq": 4, "kind": "note", "text": "n4"})
    expected = 4 if active_tail else 3
    assert [e.cursor for e in journal.read_since(0)[0]] == list(range(1, expected + 1))
    reloaded = comms.Journal(path)
    assert reloaded.cursor == expected
    assert [e.cursor for e in reloaded.read_since(0)[0]] == list(range(1, expected + 1))
    assert reloaded.question_states("s")[question["id"]]["state"] == "open"
    assert reloaded.append("s", question) is None
    assert reloaded.append("s", {"id": "s/new", "kind": "note"}).cursor == expected + 1


def test_question_older_than_both_generations_survives_rotation(tmp_path, monkeypatch):
    monkeypatch.setattr(comms, "MEMORY_EVENTS", 1)
    monkeypatch.setattr(comms, "MAX_JOURNAL_BYTES", 1)
    path = tmp_path / "journal.jsonl"
    journal = comms.Journal(path)
    question = {"id": "s/q", "kind": "question", "text": "branch?"}
    journal.append("s", question)
    journal.append("s", {"id": "s/n", "kind": "note"})
    journal.append("s", {"id": "s/n2", "kind": "note"})
    reloaded = comms.Journal(path)
    assert reloaded.cursor == 3
    assert reloaded.question_states("s")["s/q"]["state"] == "open"
    reloaded.append("s", {"id": "s/a", "kind": "answer", "in_reply_to": "s/q"})
    assert comms.Journal(path).question_states("s")["s/q"]["state"] == "answered"


def test_assistant_bus_posts_remain_assistant_turns_in_the_chat_pane(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from lmer_platform import transcripts

    log = bus.MessageLog("assistant", agent_channel=bus.CHANNEL_UBER)
    incoming = log.append(kind="message", text="hello", source=bus.Source("slack", author="Alice"))[0]
    outgoing = log.append(kind="message", text="hello back", source=bus.Source("agent"), session_caller=True)[0]
    journal = comms.Journal(tmp_path / "fleet.jsonl")
    for message in (incoming, outgoing):
        journal.append("assistant", message.to_dict())
    monkeypatch.setattr(comms, "ensure_hub", lambda: SimpleNamespace(feed=journal.read_since))
    turns, copies = transcripts._comms_messages("assistant")
    assert [(t.role, t.text, t.source["channel"]) for t in turns] == [
        ("user", "hello", "slack"), ("assistant", "hello back", "uber-lmer"),
    ]
    assert [(role, text) for role, text, _at in copies] == [
        ("user", "[slack · Alice] hello"),
        ("assistant", "hello back"),
    ], "both directions of the harness's own copy are hidden"
    assert copies[1][2] == outgoing.at, (
        "an outbound copy carries the record's time, so it is spent on the turn "
        "that produced it rather than the first that says the same words"
    )


# ---------------------------------------------------------------------------
# Issue #421: a session with a bus whose in-tree CLI predates it. Its records
# only ever reach the ask directory, and the supervisor's projection lifts them
# onto the bus — so the daemon reads one bus session and needs no rule of its
# own for the mixed case.
# ---------------------------------------------------------------------------


class TestOldPathCompatibility:
    @pytest.fixture
    def channel(self, plane, platform_root):
        """The session's ask directory, with the supervisor's projection on it.

        What a container of this release has when its own CLI is older: the
        supervisor mounts and mirrors the directory, and nothing else in the
        session ever posts to the bus.
        """
        from lmer_platform import ask as ask_channel

        session = live_session(plane)
        directory = ask_channel.prepare_ask_dir(session)
        projection = bus.AskDirProjection(str(directory), plane.log)
        plane.log.add_listener(projection.mirror)
        return session, directory, projection

    def test_a_bus_question_is_found_by_the_file_it_is_not_by_its_number(
        self, platform_root, plane
    ):
        """An entry id and a seq are different counters. The fold is asked which
        record *is* the file, and answers from the ref or not at all."""
        session = live_session(plane)
        hub = comms.CommsHub()

        def journal(seq, text, ref=None):
            source = {"channel": "agent"}
            if ref:
                source["ref"] = ref
            hub.journal.append(session, {
                "schema": 1, "id": f"{session}/{seq:06d}", "seq": seq,
                "session": session, "at": "2026-09-06T10:00:00Z", "source": source,
                "kind": "question", "text": text,
            })

        journal(7, "mirrored?")                                  # born on the bus
        journal(9, "lifted?", ref="ask-dir:000004.question.json")  # lifted from 000004

        found = hub.question_for_ask_entry
        assert found(session, "000007", mirrored=True) == f"{session}/000007"
        assert found(session, "000004", mirrored=False) == f"{session}/000009"
        assert found(session, "000009", mirrored=False) is None, (
            "the lifted question's own seq names no ask-directory entry"
        )
        assert found(session, "000007", mirrored=False) is None, (
            "unstamped: the file is the session CLI's, whatever number it carries"
        )
        assert found(session, "000008", mirrored=True) is None

    def test_a_question_the_bus_knows_is_answered_on_the_bus(
        self, client, platform_root, plane, channel
    ):
        """The projection files every bus question here too, so the directory
        holding one is not what decides the path — the bus not knowing it is."""
        from ask_channel import protocol as ask_protocol

        session, directory, _ = channel
        question = agent_posts(plane, "question", "branch?")
        hub = comms.ensure_hub()
        hub.subscribe(session)
        _until(lambda: hub.question_for_ask_entry(
            session, f"{question.seq:06d}", mirrored=True))

        reply = client.post(
            f"/api/sessions/{session}/ask/{question.seq:06d}/answer",
            json={"answer": "prep-release"}, headers=auth(),
        )
        assert reply.status_code == 200 and reply.json()["message_id"]
        assert plane.log.question("1").state == bus.STATE_ANSWERED
        entry = ask_protocol.read_entry(directory, f"{question.seq:06d}")
        assert entry.answer.source == "platform:operator", "mirrored, not filed"

    def test_a_question_neither_side_knows_keeps_the_bus_refusal(
        self, client, platform_root, channel
    ):
        session, _, _ = channel
        reply = client.post(
            f"/api/sessions/{session}/ask/000042/answer",
            json={"answer": "x"}, headers=auth(),
        )
        assert reply.status_code == 400
        assert "names no question" in reply.json()["detail"]

    def test_an_old_path_question_is_answered_by_file_and_lifted_onto_the_bus(
        self, client, platform_root, plane, channel
    ):
        """The whole of the case in note 86853: the CLI asked through the ask
        directory, the operator answered through the daemon, and the record of
        both ends up on the bus."""
        from ask_channel import protocol as ask_protocol

        session, directory, projection = channel
        question = ask_protocol.post_question(directory, "which branch?", ["prep"])

        reply = client.post(
            f"/api/sessions/{session}/ask/{question.id}/answer",
            json={"answer": "prep-release"}, headers=auth(),
        )
        assert reply.status_code == 200, reply.text
        answer = ask_protocol.answer_for(directory, question)
        assert answer.text == "prep-release", "the waiting CLI polls this file"

        lifted = projection.lift_foreign_records()
        assert [(m.kind, m.text) for m, _ in lifted] == [
            ("question", "which branch?"), ("answer", "prep-release"),
        ]
        assert plane.log.question(lifted[0][0].id).state == bus.STATE_ANSWERED
        assert plane.sink == [], "the file is the delivery, not the PTY"

    def _diverged(self, plane, channel, client):
        """Two old-path questions on a bus whose seq already runs ahead of the
        directory — the state the arithmetic used to get wrong. Returns both."""
        from ask_channel import protocol as ask_protocol

        session, directory, projection = channel
        plane.log.append(kind="message", text="how is it going?",
                         source=bus.Source("uber-lmer", ref="u1"))
        first = ask_protocol.post_question(directory, "q1?")
        second = ask_protocol.post_question(directory, "q2?")
        lifted = [m for m, _ in projection.lift_foreign_records()]
        assert [(m.seq, m.source.ref) for m in lifted] == [
            (2, "ask-dir:000001.question.json"), (3, "ask-dir:000002.question.json"),
        ], "the second question's entry id is 000002 and its seq is 3"
        hub = comms.ensure_hub()
        hub.subscribe(session)
        _until(lambda: len(hub.journal.question_states(session)) == 2)
        return session, directory, first, second

    def test_an_answer_lands_on_the_question_the_listing_named(
        self, client, platform_root, plane, channel
    ):
        """`000002` is the second question in the directory and the *first* one
        on the bus. Answering by the number bound the reply to `q1?`."""
        from ask_channel import protocol as ask_protocol

        session, directory, first, second = self._diverged(plane, channel, client)
        reply = client.post(
            f"/api/sessions/{session}/ask/{second.id}/answer",
            json={"answer": "prep-release"}, headers=auth(),
        )
        assert reply.status_code == 200, reply.text
        assert ask_protocol.answer_for(directory, second).text == "prep-release"
        assert ask_protocol.answer_for(directory, first) is None, (
            "the question nobody answered has no answer"
        )

    def test_an_open_question_is_not_refused_for_another_one_s_answer(
        self, client, platform_root, plane, channel
    ):
        """The same arithmetic, one step later: with `q1?` answered, the record
        at seq 2 is answered, and `000002` was refused as already answered —
        for a question `GET …/ask` was listing as open."""
        from ask_channel import protocol as ask_protocol

        session, directory, first, second = self._diverged(plane, channel, client)
        assert client.post(
            f"/api/sessions/{session}/ask/{first.id}/answer",
            json={"answer": "a1"}, headers=auth(),
        ).status_code == 200
        # The lift is what makes the *bus* question answered, which is what the
        # refusal this test is about was reading. Without it the bus question is
        # still open and only the wrong-file half of the bug shows.
        channel[2].lift_foreign_records()
        _until(lambda: [
            q for q in comms.ensure_hub().questions() if q["state"] == "answered"
        ])
        listed = client.get(f"/api/sessions/{session}/ask", headers=auth()).json()
        assert [e["answered"] for e in listed["entries"]] == [True, False]

        reply = client.post(
            f"/api/sessions/{session}/ask/{second.id}/answer",
            json={"answer": "a2"}, headers=auth(),
        )
        assert reply.status_code == 200, reply.text
        assert ask_protocol.answer_for(directory, second).text == "a2"

    def test_a_file_the_session_wrote_is_not_taken_for_the_bus_record_beside_it(
        self, client, platform_root, plane, channel
    ):
        """The last place identity was inferred. A bus note takes `000001`, so
        the CLI's next entry is `000002`; the bus's next question is seq 2, its
        mirror write of `000002.question.json` is refused, and the file stays the
        CLI's — same id, and the same words if the two happen to ask the same
        thing. The stamp is what tells them apart."""
        from ask_channel import protocol as ask_protocol

        session, directory, projection = channel
        agent_posts(plane, "note", "cloning")                    # 000001.note.json
        question = ask_protocol.post_question(directory, "which branch?")
        assert question.id == "000002"
        bus_question = agent_posts(plane, "question", "which branch?")
        assert bus_question.seq == 2, "and its mirror write is refused"
        assert ask_protocol.read_entry(directory, "000002").nonce, "the CLI's file"

        hub = comms.ensure_hub()
        hub.subscribe(session)
        _until(lambda: len(hub.journal.question_states(session)) == 1)
        reply = client.post(
            f"/api/sessions/{session}/ask/{question.id}/answer",
            json={"answer": "prep-release"}, headers=auth(),
        )
        assert reply.status_code == 200, reply.text
        assert ask_protocol.answer_for(directory, question).text == "prep-release"
        assert plane.log.question(str(bus_question.seq)).state == bus.STATE_OPEN, (
            "the bus question with the same number and the same words is untouched"
        )

    def test_a_closed_old_path_question_is_refused_with_the_reason_it_was_closed(
        self, client, platform_root, plane, channel
    ):
        """The session gave up and said why. "Already answered" names an event
        that did not happen and drops the one thing the operator can act on."""
        from ask_channel import protocol as ask_protocol

        session, directory, projection = channel
        question = ask_protocol.post_question(directory, "which branch?")
        ask_protocol.close_question(directory, question, reason="took prep-release")
        projection.lift_foreign_records()
        hub = comms.ensure_hub()
        hub.subscribe(session)
        _until(lambda: hub.journal.question_states(session))

        reply = client.post(
            f"/api/sessions/{session}/ask/{question.id}/answer",
            json={"answer": "main"}, headers=auth(),
        )
        assert reply.status_code == 409
        detail = reply.json()["detail"]
        assert "took prep-release" in detail and "stopped waiting" in detail
        assert "already answered" not in detail

    def test_an_old_path_milestone_becomes_exactly_one_digest(
        self, platform_root, channel, monkeypatch
    ):
        from ask_channel import protocol as ask_protocol
        from lmer_platform import assistant, daemon, detect

        session, directory, projection = channel
        spooled = []
        monkeypatch.setattr(
            assistant, "notify",
            lambda note, kind="event", data=None: spooled.append((kind, note)) or True,
        )
        hub = comms.CommsHub()
        hub.add_consumer(daemon._assistant_consumer(hub))
        hub.subscribe(session)
        comms.register_hub(hub)

        ask_protocol.post_signal(directory, "opened MR !294")
        assert len(projection.lift_foreign_records()) == 1
        _until(lambda: [item for item in spooled if item[0] == "session_signal"])
        hub.stop()

        signals = [note for kind, note in spooled if kind == "session_signal"]
        assert len(signals) == 1 and "signalled: opened MR !294" in signals[0]
        assert projection.lift_foreign_records() == [], "the file is spent"

    def test_a_lifted_milestone_is_marked_under_the_stem_it_was_filed_under(
        self, platform_root, plane, channel, monkeypatch
    ):
        """The mark the two readers share counts ask-directory entries, and the
        bus seq counts something else: neither leads, so a mark kept in the wrong
        one is wrong in both directions. This fixture builds the direction where
        it is set too high, and the *next* old-path milestone — filed one past
        the directory, so at or below it — is dropped on daemon recovery with
        nothing logged."""
        from ask_channel import protocol as ask_protocol
        from lmer_platform import assistant, daemon, detect

        session, directory, projection = channel
        monkeypatch.setattr(
            assistant, "notify", lambda note, kind="event", data=None: True
        )
        # What made the two counters diverge in the session this came from: the
        # bus holds records the ask directory never sees, so the file's stem
        # runs behind the seq a lift is given.
        plane.log.append(kind="message", text="how is it going?",
                         source=bus.Source("uber-lmer", ref="u1"))
        ask_protocol.post_signal(directory, "opened MR !294")

        hub = comms.CommsHub()
        hub.add_consumer(daemon._assistant_consumer(hub))
        hub.subscribe(session)
        comms.register_hub(hub)
        ((lifted, _),) = projection.lift_foreign_records()
        assert (lifted.seq, lifted.source.ref) == (2, "ask-dir:000001.signal.json")
        _until(lambda: detect._signal_marks().get(session))
        hub.stop()  # a restart: the detector reads the directory again

        assert detect._signal_marks()[session] == 1, "the file's stem, not the bus seq"

        # The signal the CLI files next: id 2 in the directory, at or below the
        # mark a seq of 2 would have left. It has been delivered by nobody.
        second = ask_protocol.post_signal(directory, "and the branch is pushed")
        assert second.id == "000002"
        assert [s.text for s in detect.new_signals()] == ["and the branch is pushed"]


def _until(condition, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("condition never held")


@pytest.mark.parametrize("kind", ["message", "note", "question", "milestone"])
@pytest.mark.parametrize("source, meta", [("uber-lmer", {}), ("agent", {"session_origin": True})])
@pytest.mark.parametrize("session", ["s-assistant", "s-worker"])
def test_assistant_origin_records_remain_in_journal_without_spooling(
    platform_root, monkeypatch, kind, source, meta, session,
):
    from lmer_platform import assistant, daemon, detect
    spooled = []
    monkeypatch.setattr(assistant, "notify", lambda *args, **kwargs: spooled.append((args, kwargs)))
    hub = comms.CommsHub()
    hub.add_consumer(daemon._assistant_consumer(hub))
    message = {"id": session + "/000001", "seq": 1, "kind": kind, "text": "own words",
               "source": {"channel": source}, "meta": meta}
    hub._ingest(session, message)
    assert hub.flush_digests() == 0
    assert spooled == []
    assert hub.journal.read_since(0)[0][0].message == message
    events = [event for event in store.read_events() if event["type"] == detect.SESSION_SIGNALLED]
    assert len(events) == int(kind == "milestone")


@pytest.mark.parametrize("kind", ["message", "milestone"])
@pytest.mark.parametrize("source", ["agent", "slack", "platform"])
def test_worker_and_human_records_still_reach_the_assistant_spool(
    platform_root, monkeypatch, kind, source,
):
    from lmer_platform import assistant, daemon
    spooled = []
    monkeypatch.setattr(assistant, "notify", lambda *args, **kwargs: spooled.append((args, kwargs)))
    hub = comms.CommsHub()
    hub.add_consumer(daemon._assistant_consumer(hub))
    hub._ingest("s-worker", {
        "id": "s-worker/000001", "seq": 1, "kind": kind, "text": "news",
        "source": {"channel": source}, "meta": {"session_origin": False},
    })
    hub.flush_digests()
    assert len(spooled) == 1
    assert spooled[0][1]["kind"] == ("session_signal" if kind == "milestone" else "session_comms")


@pytest.mark.parametrize("source, meta", [("uber-lmer", {}), ("agent", {"session_origin": True})])
def test_assistant_milestone_is_recorded_and_marked_from_the_real_bus(
    platform_root, plane, monkeypatch, source, meta,
):
    from lmer_platform import assistant, daemon, detect
    session = live_session(plane, kind=assistant.KIND)
    spooled = []
    monkeypatch.setattr(assistant, "notify", lambda *args, **kw: spooled.append(args))
    hub = comms.CommsHub()
    hub.add_consumer(daemon._assistant_consumer(hub))
    hub.subscribe(session)
    try:
        plane.log.agent_channel = "uber-lmer"
        message, _ = plane.log.append(
            kind="milestone", text="own milestone",
            source=bus.Source(source, ref="ask-dir:000001.signal.json"),
            session_caller=bool(meta),
        )
        assert message.source.channel == "uber-lmer"
        assert bool(message.meta.get("session_origin")) == bool(meta)
        _until(lambda: hub.journal.read_since(0)[0])
        _until(lambda: detect._signal_marks().get(session) == 1, timeout=2)
        events = [event for event in store.read_events() if event["type"] == detect.SESSION_SIGNALLED]
        assert len(events) == 1
        assert hub.flush_digests() == 0
        assert spooled == []
    finally:
        hub.stop()


def test_assistant_milestone_projection_does_not_echo_when_the_hub_is_absent(
    platform_root, plane, monkeypatch,
):
    from lmer_platform import assistant, detect, ask as ask_channel
    from ask_channel import protocol as ask_protocol
    session = live_session(plane, kind=assistant.KIND)
    monkeypatch.setattr(comms, "_HUB", None)
    directory = ask_channel.session_ask_dir(session)
    directory.mkdir(parents=True, exist_ok=True)
    ask_protocol.write_record(directory, "000001" + ask_protocol.SIGNAL_SUFFIX, {
        "schema": ask_protocol.SCHEMA_VERSION, "id": "000001",
        "kind": ask_protocol.KIND_SIGNAL, "text": "own milestone",
        "at": "2026-09-04T00:00:00Z",
    })
    assert [signal.text for signal in detect.new_signals()] == ["own milestone"]
    spooled = []
    detector = detect.Detector(cfg.load(), notifier=lambda *args, **kw: spooled.append(args))
    assert [signal.text for signal in detector._ingest_signals()] == ["own milestone"]
    assert spooled == []
    assert detect._signal_marks()[session] == 1
    assert detect.new_signals() == []
    events = [event for event in store.read_events() if event["type"] == detect.SESSION_SIGNALLED]
    assert len(events) == 1
    registry.update(session, kind="worker")
    ask_protocol.post_signal(directory, "worker milestone")
    assert [signal.text for signal in detector._ingest_signals()] == ["worker milestone"]
    assert len(spooled) == 1


def test_each_thread_loss_reaches_the_real_bus_after_reconnect(plane, platform_root, tmp_path, monkeypatch):
    from tests.test_platform_thread_connectors import SLACK_LINK, component
    from slack_chat import registry as thread_registry

    monkeypatch.setattr(thread_registry, "REGISTRY_DIR", str(tmp_path / "threads"))
    session = live_session(plane)
    hub = comms.CommsHub()
    monkeypatch.setattr(comms, "ensure_hub", lambda: hub)
    now = [0.0]
    threads = component(platform_root, clock=lambda: now[0])
    try:
        for count in (1, 2):
            connected = threads.connect(session, SLACK_LINK, by="operator")
            threads.disconnect(session, connected["id"])
            messages = plane.log.read_since(0)[0]
            assert len(messages) == count
            assert b"".join(plane.sink).count(b"thread disconnected:") == count
        live_session(plane, session_id="holder")
        threads.connect("holder", SLACK_LINK, by="operator")
        threads.adopt_launch_targets(session, [SLACK_LINK], by="operator")
        for _ in range(2):
            now[0] += 100
            threads.reconcile()
        messages = plane.log.read_since(0)[0]
        assert len(messages) == 3
        assert len({message.id for message in messages}) == 3
        assert all(message.source.channel == "platform" for message in messages)
        assert b"".join(plane.sink).count(b"thread disconnected:") == 3
    finally:
        threads.stop()
