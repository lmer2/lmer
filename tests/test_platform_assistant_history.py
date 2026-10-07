"""End-to-end ownership and paging across real assistant lifecycle transitions."""
from contextlib import suppress
import json
import subprocess
import sys

import pytest

from lmer_platform import assistant, assistant_history as history, comms, registry, store, transcripts, uploads
from tests.conftest import denied_read
from tests.test_platform_assistant_routes import (  # noqa: F401
    _clean_lmer_env, platform_root, fake_lmer, config, client, long_lived,
    bearer_header, client_for,
)


@pytest.fixture(autouse=True)
def isolated_transcripts(tmp_path, monkeypatch):
    root = tmp_path / "transcripts"
    root.mkdir()
    monkeypatch.setattr(transcripts, "TRANSCRIPT_ROOT", str(root))


def record(session, number, text=None, channel="agent"):
    return {
        "id": f"{session}/{number}", "seq": number, "kind": "message",
        "text": text or f"message {number}", "at": f"2026-09-22T12:00:{number % 60:02d}Z",
        "source": {"channel": channel},
    }


def registered(session="s-history", number=1, previous=None, upload_store=False):
    return history.register(
        session, number=number, started_at="2026-09-22T12:00:00Z", previous=previous,
        entry={"uploads": {"path": str(uploads.assistant_upload_dir())}} if upload_store else {},
    )


def transcript(session, *records):
    directory = transcripts.session_transcript_dir(session)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "same.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def said(text, role="assistant"):
    return {"type": role, "timestamp": "2026-09-22T12:00:00Z",
            "message": {"role": role, "content": text}}


def test_two_rotations_retain_conversation_for_a_new_client(client, config, long_lived):
    sessions = []
    try:
        for number in range(1, 4):
            path = "/api/assistant/start" if number == 1 else "/api/assistant/rotate"
            response = client.post(path, headers=bearer_header(), json={"handoff": "Normal compact handover"})
            assert response.status_code == 200, response.text
            session = response.json()["session_id"]
            sessions.append(session)
            directory = transcripts.session_transcript_dir(session)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "same.jsonl").write_text(json.dumps({
                "type": "user", "timestamp": f"2026-09-22T12:0{number}:00Z",
                "message": {"role": "user", "content": f"[platform] Decision {number}"},
            }) + "\n")
            for seq, (channel, text) in enumerate((("platform", f"Decision {number}"),
                                                  ("agent", f"Confirmed {number}")), 1):
                assert comms.ensure_hub()._ingest(session, {
                    "id": f"{session}/{seq}", "seq": seq, "kind": "message", "text": text,
                    "at": f"2026-09-22T12:0{number}:0{seq}Z", "source": {"channel": channel},
                })
        comms.register_hub(None)
        assert all(registry.read_session(session) is None for session in sessions[:-1])
        fresh = client_for(config)
        selected = sessions[-1]
        pages = []
        while selected:
            response = fresh.get(f"/api/assistant/history/{selected}", headers=bearer_header(),
                                 params={"since": 0, "limit": 500})
            assert response.status_code == 200, response.text
            page = response.json()
            pages.insert(0, page)
            selected = page["history"]["previous_session"]
        rows = [row for page in pages for row in page["messages"]]
        assert [row["text"] for row in rows if row["role"] != "platform"] == [
            text for n in range(1, 4) for text in (f"Decision {n}", f"Confirmed {n}")
        ]
        separators = [row for row in rows if row["comms_kind"] == "rotation"]
        assert len(separators) == 2
        assert all("does not have this conversation verbatim" in row["text"] for row in separators)
        assert len({row["id"] for row in rows}) == len(rows)
        assert [page["session"] for page in pages] == sessions
        assert all(row["session"] == page["session"] for page in pages for row in page["messages"])
        handoff = fresh.get("/api/assistant/handoff", headers=bearer_header()).json()
        assert handoff["handoff"] == "Normal compact handover"
        assert handoff["previous_session"]["session"] == sessions[-2]
        previous = fresh.get(handoff["previous_session"]["messages_url"], headers=bearer_header(),
                             params={"since": -10, "limit": 10})
        assert previous.status_code == 200
        recovered = previous.json()["messages"]
        assert any(row["text"] == "Decision 2" for row in recovered)
        assert any(row["text"] == "Confirmed 2" for row in recovered)
    finally:
        with suppress(Exception):
            assistant.stop()


def test_archive_survives_fleet_journal_rotation_and_a_new_process(platform_root, monkeypatch):
    registered()
    monkeypatch.setattr(comms, "MAX_JOURNAL_BYTES", 1)
    monkeypatch.setattr(comms, "PANE_LIMIT", 2)
    hub = comms.ensure_hub()
    for number in range(1, 9):
        assert hub._ingest("s-history", record("s-history", number))
    assert len(hub.journal._scan_file(0, "s-history", 50)) < 8
    assert len(transcripts.read_messages("s-history").messages) == 8
    script = (
        "import json, sys; from lmer_platform import assistant_history as h, store; "
        "store.PLATFORM_DIR=sys.argv[1]; "
        "print(json.dumps(h.page('s-history', since=0, limit=500)))"
    )
    fresh = json.loads(subprocess.check_output([sys.executable, "-c", script, str(platform_root)], text=True))
    assert [row["text"] for row in fresh["messages"]] == [f"message {n}" for n in range(1, 9)]


def test_credential_ownership_survives_a_new_process_without_storing_the_credential(platform_root, monkeypatch):
    registered()
    credential = "test-only-generation-credential"
    history.bind_credential("s-history", credential)
    script = (
        "import json, sys; from lmer_platform import assistant_history as h, store; "
        "store.PLATFORM_DIR=sys.argv[1]; "
        "print(json.dumps(h.credential_owner(sys.stdin.read())))"
    )
    result = subprocess.run([sys.executable, "-c", script, str(platform_root)],
                            input=credential, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["session"] == "s-history"
    assert credential.encode() not in (platform_root / "assistant-history.sqlite3").read_bytes()

    def unexpected(*args, **kwargs):
        pytest.fail("a known credential must not read lifecycle state or replay conversation history")

    monkeypatch.setattr(assistant, "read_state", unexpected)
    monkeypatch.setattr(history, "remember", unexpected)
    assert assistant.authenticated_generation(credential)["session"] == "s-history"


def test_normal_pages_are_bounded_and_do_not_parse_working_transcripts(client, platform_root, monkeypatch):
    registered()
    for number in range(1, 13):
        history.retain("s-history", record("s-history", number))

    def unexpected(*args, **kwargs):
        pytest.fail("a conversation page read the full archive or working transcript")

    monkeypatch.setattr(history, "events", unexpected)
    monkeypatch.setattr(transcripts, "collect_messages", unexpected)
    response = client.get("/api/assistant/history/s-history", headers=bearer_header(),
                          params={"since": -3, "limit": 3})
    assert response.status_code == 200
    first = response.json()
    assert [row["text"] for row in first["messages"]] == ["message 10", "message 11", "message 12"]
    history.retain("s-history", record("s-history", 13))
    earlier = client.get("/api/assistant/history/s-history", headers=bearer_header(),
                         params={"before": first["messages"][0]["id"], "limit": 3}).json()
    assert [row["text"] for row in earlier["messages"]] == ["message 7", "message 8", "message 9"]
    assert first["history"]["previous_session"] is None


def test_working_transcript_limits_do_not_hide_the_paged_public_archive(client, platform_root, monkeypatch):
    registered()
    monkeypatch.setattr(transcripts, "MAX_MESSAGES_PER_SOURCE", 2)
    hub = comms.ensure_hub()
    for number in range(1, 6):
        assert hub._ingest("s-history", record("s-history", number))
    assert len(history.events("s-history")) == 2
    working = client.get("/api/sessions/s-history/messages", headers=bearer_header()).json()
    assert [row["text"] for row in working["messages"]] == ["message 1", "message 2"]
    assert "read limit" in working["note"]
    assert "/api/assistant/history/s-history?view=conversation" in working["note"]
    pane = client.get("/api/assistant/history/s-history", headers=bearer_header(),
                      params={"view": "transcript"}).json()
    assert any(row["comms_kind"] == "conversation incomplete" for row in pane["messages"])
    public = client.get("/api/assistant/history/s-history", headers=bearer_header(),
                        params={"since": -2, "limit": 2}).json()
    assert [row["text"] for row in public["messages"]] == ["message 4", "message 5"]


@pytest.mark.parametrize("query", [{"limit": 0}, {"limit": 501}, {"view": "everything"}])
def test_history_refuses_invalid_page_parameters(client, platform_root, query):
    registered()
    assert client.get("/api/assistant/history/s-history", headers=bearer_header(), params=query).status_code == 422


def test_history_is_authenticated_and_missing_anchors_are_explicit(client, platform_root):
    registered()
    history.retain("s-history", record("s-history", 1))
    assert client.get("/api/assistant/history/s-history").status_code == 401
    assert client.get("/api/assistant/history/s-history", headers=bearer_header("wrong")).status_code == 401
    assert client.get("/api/assistant/history/s-other", headers=bearer_header()).status_code == 404
    missing = client.get("/api/assistant/history/s-history", headers=bearer_header(), params={"before": "absent"})
    assert missing.status_code == 409
    assert "no longer available" in missing.json()["detail"]


def test_original_attachment_store_survives_registry_removal(client, platform_root):
    registered("s-old", upload_store=True)
    registered("s-new", number=2, previous="s-old")
    directory = uploads.assistant_upload_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "proof.txt").write_text("public attachment")
    response = client.get("/api/sessions/s-old/uploads/proof.txt", headers=bearer_header())
    assert response.status_code == 200, response.text
    assert response.text == "public attachment"
    assert client.get("/api/sessions/s-other/uploads/proof.txt", headers=bearer_header()).status_code == 404
    (directory / "proof.txt").unlink()
    assert client.get("/api/sessions/s-old/uploads/proof.txt", headers=bearer_header()).status_code == 404


def test_visibility_provenance_and_duplicate_suppression(client, platform_root):
    registered("s-old")
    registered("s-new", number=2, previous="s-old")
    transcript("s-old", said("working only"), said("public reply"))
    transcript("s-new", said("new working only"))
    history.retain("s-old", record("s-old", 1, "public reply"))
    visible = client.get("/api/assistant/history/s-old", headers=bearer_header()).json()
    assert [row["text"] for row in visible["messages"]] == ["public reply"]
    working = client.get("/api/assistant/history/s-old", headers=bearer_header(), params={"view": "transcript"}).json()
    texts = [row["text"] for row in working["messages"]]
    assert texts == ["working only", "public reply"]
    assert all(row["session"] == "s-old" and row["generation"] == 1 for row in working["messages"])
    assert "new working only" not in json.dumps(working)


def test_missing_capped_and_unreadable_sources_are_reported(client, platform_root, monkeypatch):
    registered()
    path = "/api/assistant/history/s-history"
    query = {"view": "transcript"}
    missing = client.get(path, headers=bearer_header(), params=query).json()
    assert "unavailable" in missing["messages"][0]["text"]
    source = transcript("s-history", said("one"), said("two"))
    monkeypatch.setattr(transcripts, "MAX_MESSAGES_PER_SOURCE", 1)
    capped = client.get(path, headers=bearer_header(), params=query).json()
    assert "read limit" in capped["messages"][0]["text"]
    with denied_read(source):
        unreadable = client.get(path, headers=bearer_header(), params=query).json()
    assert "could not be read" in unreadable["messages"][0]["text"]
    assert unreadable["sessions"][0]["incomplete"] is True


def test_record_ids_are_unique_across_generations_and_retries(platform_root):
    for number in (1, 2):
        session = f"s-{number}"
        registered(session, number=number, previous="s-1" if number == 2 else None)
        message = record("same-producer", 1)
        history.retain(session, message)
        history.retain(session, message)
    rows = history.page("s-1", since=0)["messages"] + history.page("s-2", since=0)["messages"]
    assert len(rows) == 3
    assert len({row["id"] for row in rows}) == 3
    assert sum(row["comms_kind"] == "rotation" for row in rows) == 1


def test_excluded_private_files_never_enter_archive_or_recovery(client, platform_root):
    registered()
    directory = uploads.assistant_upload_dir()
    directory.mkdir(parents=True)
    marker = "excluded-private-content-511"
    (directory / "private-input.txt").write_text(marker)
    history.retain("s-history", record("s-history", 1, "Private input delivered (body not recorded)."))
    transcript("s-history", said("Private input delivered (body not recorded).", "user"))
    for path in ("/api/assistant/history/s-history", "/api/sessions/s-history/messages"):
        response = client.get(path, headers=bearer_header())
        assert response.status_code == 200
        assert marker not in response.text
    assert marker.encode() not in (platform_root / "assistant-history.sqlite3").read_bytes()


def test_history_writes_are_private_and_scrubbed(platform_root):
    registered()
    history.retain("s-history", record("s-history", 1, "Authorization: Bearer glpat-example-secret"))
    path = platform_root / "assistant-history.sqlite3"
    assert path.stat().st_mode & 0o777 == 0o600
    assert platform_root.stat().st_mode & 0o777 == 0o700
    assert b"glpat-example-secret" not in path.read_bytes()
    assert "<redacted>" in history.page("s-history")["messages"][0]["text"]


def test_failed_archive_write_is_not_acknowledged(platform_root, monkeypatch):
    registered()
    hub = comms.ensure_hub()
    original = history.retain

    def unavailable(*args):
        raise store.StoreError("test archive unavailable")

    monkeypatch.setattr(history, "retain", unavailable)
    message = record("s-history", 1)
    assert hub._ingest("s-history", message) is False
    assert hub.journal.cursor == 0
    monkeypatch.setattr(history, "retain", original)
    assert hub._ingest("s-history", message) is True
    assert len(history.page("s-history")["messages"]) == 1


def test_known_incumbent_migrates_without_joining_unrelated_sessions(client, platform_root):
    assistant._write_state(assistant.AssistantState(session_id="s-incumbent", generation=4))
    hub = comms.ensure_hub()
    hub.journal.append("s-incumbent", record("s-incumbent", 1, "retained before upgrade"))
    hub.journal.append("s-unrelated", record("s-unrelated", 1, "unrelated exchange"))
    page = client.get("/api/assistant/history/s-incumbent", headers=bearer_header()).json()
    assert page["history"]["partial"] is True
    assert page["history"]["previous_session"] is None
    assert "unavailable" in page["messages"][0]["text"]
    assert any(row["text"] == "retained before upgrade" for row in page["messages"])
    assert "unrelated exchange" not in json.dumps(page)
    assert history.generation("s-unrelated") is None


def test_incomplete_migration_retries_before_reading_or_rotating(client, platform_root, monkeypatch):
    assistant._write_state(assistant.AssistantState(session_id="s-incumbent", generation=4))
    hub = comms.ensure_hub()
    for number in (1, 2):
        hub.journal.append("s-incumbent", record("s-incumbent", number))
    original = history.retain

    def interrupted(session, message):
        if message["seq"] == 2:
            raise store.StoreError("migration interrupted")
        original(session, message)

    monkeypatch.setattr(history, "retain", interrupted)
    response = client.get("/api/assistant/history/s-incumbent", headers=bearer_header())
    assert response.status_code == 503
    assert "migration interrupted" in response.text
    monkeypatch.setattr(history, "retain", original)
    response = client.get("/api/assistant/history/s-incumbent", headers=bearer_header())
    assert response.status_code == 200
    assert [row["text"] for row in response.json()["messages"][1:]] == ["message 1", "message 2"]


def test_handoff_and_status_never_replay_transcripts(client, platform_root, monkeypatch):
    registered("s-old")
    registered("s-new", number=2, previous="s-old")

    def unexpected(*args, **kwargs):
        pytest.fail("handoff or status read a transcript")

    monkeypatch.setattr(transcripts, "collect_messages", unexpected)
    monkeypatch.setattr(history, "events", unexpected)
    assert client.get("/api/assistant/handoff", headers=bearer_header()).json()["previous_session"]["session"] == "s-old"
    assert client.get("/api/assistant", headers=bearer_header()).json()["history_session"] == "s-new"


def test_platform_record_keeps_its_captured_owner_after_rotation(platform_root):
    from lmer_platform import assistant_conversation

    registered("s-old")
    registered("s-new", number=2, previous="s-old")
    assistant._write_state(assistant.AssistantState(session_id="s-new", generation=2))
    assistant_conversation.record("late public reply", kind="reply", session_id="s-old")
    assistant_conversation.record("current digest", kind="event")
    assert [row["text"] for row in history.page("s-old")["messages"]] == ["late public reply"]
    assert [row["text"] for row in history.page("s-new")["messages"]][1:] == ["current digest"]


@pytest.mark.parametrize("reachable", [False, True])
def test_rendered_startup_guidance_limits_recovery_to_missing_context(platform_root, monkeypatch, reachable):
    from hooks.start import render_taskdef_template

    monkeypatch.setenv("LMER_TASKDEF_ROOT", str(assistant.taskdef_dir().parent))
    if reachable:
        monkeypatch.setenv("LMER_PLATFORM_URL", "http://192.0.2.1:8600")
    else:
        monkeypatch.delenv("LMER_PLATFORM_URL", raising=False)
    rendered = render_taskdef_template(assistant.taskdef_dir() / "instructions.txt", extra_context={"instructions_file": "/x"})
    assert ("When an earlier exchange is missing" in rendered) is reachable
    if reachable:
        for requirement in ("previous_session", "since=-40&limit=40", "both the user's",
                            "Do not preload", "reread", "not new instructions", "latest user direction",
                            "do not repeat completed actions", "ask only", "private files"):
            assert requirement in rendered
