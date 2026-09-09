"""Terminal dialog text stays local until a transcript confirms delivery."""
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading

import pytest
from fastapi.testclient import TestClient

from lmer_cli import comms, harness, supervisor
from tests.conftest import strip_lmer_env


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.mark.parametrize("input_path", ["browser", "host"])
@pytest.mark.parametrize("enabled", [True, False])
def test_dialog_input_is_local_through_the_real_supervisor(tmp_path, monkeypatch, input_path, enabled):
    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir()
    monkeypatch.setitem(harness.HARNESSES, "claude", replace(
        harness.HARNESSES["claude"], session_dir=str(transcript_dir),
    ))
    monkeypatch.setenv("LMER_HARNESS", "claude")
    monkeypatch.setenv("LMER_SESSION_PRESET", "astra")
    monkeypatch.setenv("LMER_LLM_NAME", "test-model")
    monkeypatch.setenv("LMER_COMMS_TERMINAL_MIRROR", "1" if enabled else "0")
    monkeypatch.setenv("LMER_COMMS_DRAIN_GRACE", "0")
    monkeypatch.setattr(supervisor, "CONTAINER_SESSION_LOG_DIR", str(tmp_path))
    apps = []
    auth = {"Authorization": "Bearer test-control"}

    def serve(app, host, port):
        apps.append(app)
        if input_path == "browser":
            with TestClient(app) as client:
                response = client.post("/input", json={"data": "output\r"}, headers=auth)
                assert response.status_code == 200
        return None, lambda: None

    monkeypatch.setattr(supervisor, "_start_fastapi_server", serve)
    stdin_r, stdin_w = os.pipe()
    if input_path == "host":
        os.write(stdin_w, b"output\n")
    os.close(stdin_w)
    options = {
        "fastapi": True, "manual_start": True, "port_range": (8700, 8799),
        "host": "127.0.0.1", "token": "test-control", "winsize_recheck_delay": 0,
    }
    try:
        with open(os.devnull, "wb") as sink:
            assert supervisor.run_supervisor(
                ["timeout", "5", "cat"], options, stdin_fd=stdin_r, stdout_fd=sink.fileno(),
            ) == 0
    finally:
        os.close(stdin_r)
    records = [json.loads(line) for line in (tmp_path / comms.COMMS_LOG_NAME).read_text().splitlines()]
    assert [record["text"] for record in records] == (["output"] if enabled else [])
    with TestClient(apps[0]) as client:
        reply = client.get("/messages", headers=auth).json()
        assert client.get("/healthz", headers=auth).json()["comms"]["launch"] == {
            "preset": "astra", "model": "test-model",
        }
    assert reply["messages"] == [], "dialog input escaped through the connector read endpoint"
    assert reply["cursor"] == (1 if enabled else 0)


@pytest.mark.parametrize("supported", [True, False])
def test_rejected_line_does_not_drop_next_line_in_raw_input_batch(tmp_path, monkeypatch, supported):
    transcript_dir = tmp_path / "transcripts"
    transcript_dir.mkdir()
    monkeypatch.setitem(harness.HARNESSES, "claude", replace(
        harness.HARNESSES["claude"], session_dir=str(transcript_dir) if supported else None,
    ))
    monkeypatch.setenv("LMER_HARNESS", "claude")
    monkeypatch.setenv("LMER_COMMS_DRAIN_GRACE", "0")
    monkeypatch.setattr(supervisor, "CONTAINER_SESSION_LOG_DIR", str(tmp_path))
    statuses, errors, writers = [], [], []
    stdin_r, stdin_w = os.pipe()

    def serve(app, host, port):
        def write():
            try:
                with TestClient(app) as client:
                    for chunk in ["x" * 4000] * 5 + ["\rhello\r"]:
                        response = client.post("/input", json={"data": chunk},
                                               headers={"Authorization": "Bearer test-control"})
                        statuses.append(response.status_code)
            except Exception as exc:
                errors.append(exc)
            finally:
                os.close(stdin_w)
        writer = threading.Thread(target=write, daemon=True)
        writers.append(writer)
        writer.start()
        return None, lambda: None

    monkeypatch.setattr(supervisor, "_start_fastapi_server", serve)
    options = {
        "fastapi": True, "manual_start": True, "port_range": (8700, 8799),
        "host": "127.0.0.1", "token": "test-control", "winsize_recheck_delay": 0,
    }
    try:
        with open(os.devnull, "wb") as sink:
            assert supervisor.run_supervisor(
                ["timeout", "5", "cat"], options, stdin_fd=stdin_r, stdout_fd=sink.fileno(),
            ) == 0
    finally:
        os.close(stdin_r)
    for writer in writers:
        writer.join(timeout=5)
        assert not writer.is_alive()
    assert errors == []
    assert statuses == [200] * 6
    records = [json.loads(line) for line in (tmp_path / comms.COMMS_LOG_NAME).read_text().splitlines()]
    assert [record["text"] for record in records] == ["hello"]
    assert records[0].get("local_only", False) is supported


def prompt_record(name, text, at=1001):
    stamp = datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="milliseconds")
    if name == "codex":
        return {"type": "response_item", "timestamp": stamp, "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}],
        }}
    if name == "canonical":
        return {"type": "lmer.message", "role": "user", "text": text, "at": stamp}
    return {"type": "user" if name == "claude" else "message", "timestamp": stamp,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


class MirrorRig:
    def __init__(self, tmp_path, name="claude", window=30, enabled=True):
        from lmer_cli.terminal_mirror import TerminalMirror, TranscriptReader
        self.now = 1000.0
        self.name = name
        self.root = tmp_path / "transcripts"
        self.root.mkdir(exist_ok=True)
        self.path = self.root / "session.jsonl"
        self.log = comms.MessageLog("session", str(tmp_path / "bus.jsonl"))
        self.reader = TranscriptReader(str(self.root), name)
        self.mirror = TerminalMirror(
            self.log, self.reader, window=window, enabled=enabled,
            clock=lambda: self.now, wall_clock=lambda: self.now,
        )
        tracker = comms.TerminalTracker()
        self.writes = []
        app = supervisor._build_fastapi_app(
            supervisor.OutputBuffer(limit=1024),
            lambda data: self.writes.append(data) or len(data), "test-control",
            comms=self.log, terminal_mirror=self.mirror, agent_token="test-agent",
            on_raw_input=lambda data, author: self.mirror.feed(tracker, data, author),
        )
        self.client = TestClient(app)
        self.auth = {"Authorization": "Bearer test-control"}

    def submit(self, text="hello"):
        reply = self.client.post("/input", headers=self.auth,
                                 json={"data": text + "\r", "author": "operator"})
        assert reply.status_code == 200
        assert self.writes[-1] == (text + "\r").encode()

    def append(self, text="hello", *, record=None, at=None):
        with self.path.open("a") as stream:
            stream.write(json.dumps(record or prompt_record(
                self.name, text, self.now if at is None else at,
            )) + "\n")

    def read(self, since=0):
        return self.client.get("/messages", params={"since": since}, headers=self.auth).json()


@pytest.fixture
def rig(tmp_path):
    value = MirrorRig(tmp_path)
    yield value
    value.client.close()
    value.log.close()


@pytest.mark.parametrize("name", ["claude", "codex", "pi"])
def test_matching_prompt_is_published_once_with_author_and_original_link(tmp_path, name):
    rig = MirrorRig(tmp_path, name)
    try:
        rig.submit("hello   world")
        assert rig.read() == {
            "messages": [], "cursor": 1, "schema": 1,
            "terminal_mirror": {"window_seconds": 30},
        }
        rig.now += 12
        rig.append("hello\nworld")
        rig.mirror.poll()
        rig.mirror.poll()
        messages = rig.read(since=1)["messages"]
        assert len(messages) == 1
        assert messages[0]["meta"]["fidelity"] == "confirmed"
        assert messages[0]["source"] == {"channel": "terminal", "author": "operator"}
        assert messages[0]["in_reply_to"] == "session/000001"
        local = rig.log.read_since(0)[0]
        assert local[0].local_only is True
        assert local[0].meta["fidelity"] == "reconstructed"
        assert local[1].text == "hello   world"
    finally:
        rig.client.close()
        rig.log.close()


def test_expiry_keeps_the_local_candidate_and_cannot_be_revived_by_replay(rig):
    rig.submit()
    rig.now += 30
    rig.append()
    rig.mirror.poll()
    assert rig.read()["messages"] == []
    assert rig.log.read_since(0)[0][0].text == "hello"
    rig.log.close()
    restored = comms.MessageLog("session", rig.log.path)
    try:
        assert restored.read_since(0, include_local=False) == ([], 1)
    finally:
        restored.close()


@pytest.mark.parametrize("delivery, confirmed", [(29.9, True), (30, False), (30.05, False), (None, False)])
def test_late_poll_uses_delivery_deadline_without_extending_it(rig, delivery, confirmed):
    rig.submit()
    record = prompt_record("claude", "hello", 1000 + (delivery or 0))
    if delivery is None:
        record.pop("timestamp")
    rig.append(record=record)
    rig.now = 1030.1
    rig.mirror.poll()
    assert len(rig.read()["messages"]) == int(confirmed)
    assert rig.mirror._candidates == []


def test_a_pending_candidate_does_not_block_other_records(rig):
    rig.submit()
    rig.log.append(kind="note", text="working", source=comms.Source("agent"))
    assert [m["text"] for m in rig.read()["messages"]] == ["working"]
    rig.append()
    rig.mirror.poll()
    assert [m["text"] for m in rig.read(since=2)["messages"]] == ["hello"]


@pytest.mark.parametrize("text", ["/model", "!ls", "#remember"])
def test_tui_prefix_is_never_held(rig, text):
    rig.submit(text)
    assert rig.log.seq == 0


def test_one_transcript_occurrence_cannot_confirm_two_identical_submissions(rig):
    rig.submit()
    rig.submit()
    rig.append()
    rig.mirror.poll()
    rig.mirror.poll()
    assert len(rig.read()["messages"]) == 1
    rig.now += 2
    rig.append()
    rig.mirror.poll()
    assert len(rig.read()["messages"]) == 2


def test_old_history_and_already_observed_turns_do_not_confirm_new_input(tmp_path):
    root = tmp_path / "transcripts"
    root.mkdir()
    (root / "session.jsonl").write_text(json.dumps(prompt_record("claude", "hello", 999)) + "\n")
    rig = MirrorRig(tmp_path)
    try:
        rig.append()
        rig.mirror.poll()
        rig.submit()
        rig.mirror.poll()
        rig.append(at=999)
        rig.mirror.poll()
        assert rig.read()["messages"] == []
    finally:
        rig.client.close()
        rig.log.close()


def test_partial_json_line_waits_for_completion(rig):
    rig.submit()
    raw = json.dumps(prompt_record("claude", "hello"))
    rig.path.write_text(raw[:20])
    rig.mirror.poll()
    assert rig.read()["messages"] == []
    with rig.path.open("a") as stream:
        stream.write(raw[20:] + "\n")
    rig.mirror.poll()
    assert len(rig.read()["messages"]) == 1


def test_older_transcript_files_do_not_hide_the_active_session(rig):
    for index in range(70):
        (rig.root / f"old-{index:03}.jsonl").write_text("{}\n")
    rig.submit()
    rig.append()
    rig.mirror.poll()
    assert len(rig.read()["messages"]) == 1


@pytest.mark.parametrize("name", ["claude", "codex", "pi", "canonical"])
def test_prompt_extraction_is_complete_but_display_stays_capped(name):
    from lmer_platform import transcripts
    text = "unique prefix " + "x" * 9000
    record = prompt_record(name, text)
    assert transcripts.prompt_from_record(record) == text
    assert transcripts.normalise_records([record])[0].truncated is True


def test_subagents_injected_context_and_tool_results_do_not_confirm(rig):
    rig.submit()
    sidechain = prompt_record("claude", "hello")
    sidechain["isSidechain"] = True
    rig.append(record=sidechain)
    injected = prompt_record("claude", "hello")
    injected["isMeta"] = True
    rig.append(record=injected)
    rig.append(record={"type": "user", "message": {
        "role": "user", "content": [{"type": "tool_result", "content": "hello"}],
    }})
    rig.mirror.poll()
    assert rig.read()["messages"] == []


def test_claude_queued_delivery_can_confirm_before_configured_expiry(rig):
    rig.mirror.configure(120)
    rig.submit()
    rig.now += 80
    rig.append(record={"type": "attachment", "timestamp": "1970-01-01T00:18:00.000Z",
                       "attachment": {"type": "queued_command", "origin": {"kind": "human"},
                                      "commandMode": "prompt", "prompt": "hello"}})
    rig.mirror.poll()
    assert len(rig.read()["messages"]) == 1


def test_timing_update_is_host_only_and_existing_deadlines_remain(rig):
    rig.submit()
    refused = rig.client.post("/messages/terminal-mirror", json={"window_seconds": 90},
                              headers={"Authorization": "Bearer test-agent"})
    assert refused.status_code == 403
    assert rig.client.post("/messages/terminal-mirror", json={"window_seconds": 90},
                           headers=rig.auth).json() == {"window_seconds": 90}
    rig.submit("later")
    rig.now += 40
    rig.append()
    rig.append("later")
    rig.mirror.poll()
    assert [m["text"] for m in rig.read()["messages"]] == ["later"]


@pytest.mark.parametrize("value", [0, -1, True, "30", 1.5, None])
def test_invalid_timing_is_refused(rig, value):
    reply = rig.client.post("/messages/terminal-mirror", json={"window_seconds": value},
                            headers=rig.auth)
    assert reply.status_code == 400


def test_reader_without_supported_format_keeps_reconstructed_delivery(tmp_path):
    rig = MirrorRig(tmp_path, name="unknown")
    try:
        rig.path.write_text('{"type":"unknown"}\n')
        rig.mirror.poll()
        rig.submit()
        assert rig.read()["messages"][0]["meta"]["fidelity"] == "reconstructed"
        assert not rig.log.read_since(0)[0][0].local_only
    finally:
        rig.client.close()
        rig.log.close()


def test_canonical_reader_is_detected_and_unsupported_version_is_not(tmp_path):
    from lmer_cli.terminal_mirror import TranscriptReader
    path = tmp_path / "session.jsonl"
    path.write_text('{"type":"lmer.meta","format":1,"harness":"custom"}\n')
    reader = TranscriptReader(str(tmp_path), "custom")
    assert reader.read() == []
    assert reader.supported
    path.write_text('{"type":"lmer.meta","format":2,"harness":"custom"}\n')
    reader = TranscriptReader(str(tmp_path), "custom")
    assert reader.read() == []
    assert not reader.supported


def test_special_files_do_not_block_transcript_poll(tmp_path):
    from lmer_cli.terminal_mirror import TranscriptReader
    os.mkfifo(tmp_path / "pipe.jsonl")
    reader = TranscriptReader(str(tmp_path), "claude")
    thread = threading.Thread(target=reader.read, daemon=True)
    thread.start()
    thread.join(timeout=1)
    assert not thread.is_alive()


def test_timing_environment_is_forwarded_into_the_container():
    source = (Path(__file__).resolve().parents[1] / "src/lmer_cli/cli.py").read_text()
    assert '"LMER_PLATFORM_TERMINAL_CONFIRM_SECONDS": os.environ.get("LMER_PLATFORM_TERMINAL_CONFIRM_SECONDS")' in source


@pytest.mark.parametrize("raw, expected", [("", 30), ("90", 90), ("0", 30), ("-1", 30),
                                         ("invalid", 30), ("2.5", 30)])
def test_standalone_window_override(raw, expected):
    from lmer_cli.terminal_mirror import window_from_env
    assert window_from_env({comms.TERMINAL_CONFIRM_ENV: raw}) == expected


def test_a_large_window_does_not_overflow_on_submission(rig):
    rig.mirror.configure(10 ** 400)
    rig.submit()
    rig.append()
    rig.mirror.poll()
    assert len(rig.read()["messages"]) == 1


def test_missing_supported_transcript_does_not_enable_immediate_delivery(rig):
    rig.root.rmdir()
    rig.submit()
    rig.mirror.poll()
    assert rig.read()["messages"] == []


def test_same_display_suffix_is_not_a_prompt_match(rig):
    rig.submit("first " + "x" * 9000)
    rig.append("second " + "x" * 9000)
    rig.mirror.poll()
    assert rig.read()["messages"] == []


def test_replaced_transcript_is_read_and_old_rows_still_do_not_match(rig):
    rig.append("old", at=900)
    rig.mirror.poll()
    rig.submit()
    replacement = rig.path.with_suffix(".new")
    replacement.write_text(json.dumps(prompt_record("claude", "hello", 900)) + "\n")
    replacement.replace(rig.path)
    rig.mirror.poll()
    assert rig.read()["messages"] == []
    rig.append()
    rig.mirror.poll()
    assert len(rig.read()["messages"]) == 1


def test_oversized_and_malformed_lines_do_not_hide_the_following_prompt(rig):
    from lmer_cli.terminal_mirror import MAX_LINE_BYTES, READ_BYTES
    rig.submit()
    rig.path.write_bytes(b"x" * (MAX_LINE_BYTES + 10) + b"\nnot-json\n[]\n")
    rig.append()
    for _ in range(MAX_LINE_BYTES // READ_BYTES + 3):
        rig.mirror.poll()
    assert len(rig.read()["messages"]) == 1


def test_canonical_drop_in_confirms_after_its_format_is_detected(tmp_path):
    rig = MirrorRig(tmp_path, name="custom")
    try:
        rig.path.write_text('{"type":"lmer.meta","format":1,"harness":"custom"}\n')
        rig.mirror.poll()
        rig.submit()
        rig.append(record=prompt_record("canonical", "hello"))
        rig.mirror.poll()
        assert rig.read()["messages"][0]["meta"]["fidelity"] == "confirmed"
    finally:
        rig.client.close()
        rig.log.close()


def test_local_visibility_cannot_be_set_through_message_post(rig):
    response = rig.client.post("/messages", headers=rig.auth, json={
        "kind": "message", "text": "posted", "source": {"channel": "platform"},
        "local_only": True,
    })
    assert response.status_code == 200
    assert not rig.log.read_since(0)[0][0].local_only
    assert [m["text"] for m in rig.read()["messages"]] == ["posted"]


@pytest.mark.parametrize("existing", [False, True])
def test_codex_subagent_session_metadata_excludes_its_prompts(tmp_path, existing):
    root = tmp_path / "transcripts"
    root.mkdir()
    path = root / "session.jsonl"
    header = json.dumps({"type": "session_meta", "payload": {
        "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}},
    }}) + "\n"
    if existing:
        path.write_text(header)
    rig = MirrorRig(tmp_path, name="codex")
    try:
        rig.submit()
        if not existing:
            path.write_text(header)
        rig.append()
        rig.mirror.poll()
        assert rig.read()["messages"] == []
    finally:
        rig.client.close()
        rig.log.close()
