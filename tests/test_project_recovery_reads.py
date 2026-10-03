"""Operator recovery reads through real authentication and retained files."""
import base64
import json
import os

from fastapi.testclient import TestClient
import pytest

from ask_channel import protocol
from lmer_platform import api, ask, assistant, assistant_history, config, project_api
from lmer_platform import project_context as scope, projects, registry, spawn, store, transcripts
from tests.conftest import strip_lmer_env


def retained_session(identity, kind):
    registry.register(identity, pid=99999999, kind=kind,
                      run={"host": None, "project": None, "slug": identity})
    transcript = transcripts.session_transcript_dir(identity) / "-workspace" / "history.jsonl"
    transcript.parent.mkdir(parents=True)
    records = [
        {"type": "user", "timestamp": "2026-09-26T00:00:00Z", "message": {
            "role": "user", "content": f"Retained history for {identity}"}},
        {"type": "assistant", "timestamp": "2026-09-26T00:00:01Z", "message": {
            "role": "assistant", "content": [
                {"type": "thinking", "thinking": "private-reasoning-fixture"},
                {"type": "text", "text": "Safe output https://fixture:example-password@forge.test/repo"},
            ]}},
    ]
    transcript.write_text("".join(json.dumps(row) + "\n" for row in records))
    log = spawn.log_path_for(identity)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"Retained terminal for {identity}\n")
    private = transcript.parent / "private-input.txt"
    private.write_text("private-input-fixture")
    channel = ask.prepare_ask_dir(identity)
    question = protocol.post_question(channel, f"Retained question for {identity}", [])
    protocol.write_answer(channel, question, f"Retained answer for {identity}")
    paths = [registry.session_path(identity), transcript, private, log, *channel.iterdir()]
    return {path: path.read_bytes() for path in paths if path.is_file()}


@pytest.fixture(params=["assistant", "worker"])
def recovery(request, tmp_path, monkeypatch):
    strip_lmer_env(monkeypatch)
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    monkeypatch.setattr(transcripts, "TRANSCRIPT_ROOT", str(tmp_path / "transcripts"))
    identity = f"s-retained-{request.param}"
    original = retained_session(identity, request.param)
    projects.migrate()
    alpha, beta = [projects.create(name) for name in ("Alpha", "Beta")]
    caller = "s-alpha-assistant"
    registry.register(caller, pid=os.getpid(), kind="assistant")
    projects.bind_session(caller, alpha["id"])
    with scope.bound(alpha["id"]):
        credential = config.mint_assistant_credential()
        assistant._write_state(assistant.AssistantState(session_id=caller, generation=1))
        assistant_history.register(caller, number=1, started_at="2026-09-26T01:00:00Z", entry={})
        assistant_history.bind_credential(caller, credential)
    other = "s-beta-worker"
    registry.register(other, pid=99999999, kind="worker")
    projects.bind_session(other, beta["id"])
    connector = config.mint_connector_credential("fixture-reader", "slack")
    with TestClient(api.create_app(config.load(), "fixture-operator"),
                    headers={"Authorization": "Bearer fixture-operator"}) as client:
        yield client, identity, alpha["id"], other, credential, connector
    assert all(path.read_bytes() == before for path, before in original.items())


def test_operator_reads_retained_conversation_log_and_answered_questions(recovery):
    client, identity, *_ = recovery
    page = client.get(f"/api/sessions/{identity}/messages")
    log = client.get(f"/api/sessions/{identity}/log")
    channel = client.get(f"/api/sessions/{identity}/ask")
    assert [response.status_code for response in (page, log, channel)] == [200, 200, 200]
    assert f"Retained history for {identity}" in page.text
    assert f"Retained question for {identity}" in page.text
    assert f"Retained answer for {identity}" in page.text
    assert "Safe output https://forge.test/repo" in page.text
    assert not any(text in page.text for text in ("example-password", "private-reasoning-fixture", "private-input-fixture"))
    assert base64.b64decode(log.json()["data"]).decode() == f"Retained terminal for {identity}\n"
    entry, = channel.json()["entries"]
    assert entry["text"] == f"Retained question for {identity}"
    assert entry["answer"]["text"] == f"Retained answer for {identity}"
    assert all(response.json()["live"] is False for response in (page, log, channel))
    assert projects.session_home(identity) is None
    tail = client.get(f"/api/sessions/{identity}/messages", params={"since": -1, "limit": 1})
    assert tail.status_code == 200 and len(tail.json()["messages"]) == 1
    assert client.get(f"/api/sessions/{identity}/log", params={"source": "unknown"}).status_code == 404


@pytest.mark.parametrize("endpoint", ["messages", "log", "ask"])
def test_recovery_reads_keep_principal_scope_and_unknown_session_checks(recovery, endpoint):
    client, identity, alpha, other, credential, connector = recovery
    path = f"/api/sessions/{identity}/{endpoint}"
    for secret, status in (("wrong-fixture", 401), (credential, 409), (connector, 400)):
        result = client.get(path, headers={"Authorization": f"Bearer {secret}"})
        assert result.status_code == status, result.text
    assert client.get(path, headers={"Authorization": ""}).status_code == 401
    for project in ("general", alpha):
        for options in ({"params": {"workspace_id": project}}, {"headers": {"X-Lmer-Workspace-ID": project}}):
            result = client.get(path, **options)
            assert result.status_code == 409, result.text
    assert client.get(f"/api/sessions/s-unknown/{endpoint}").status_code == 404
    assert client.get(f"/api/sessions/{other}/{endpoint}", headers={"Authorization": f"Bearer {credential}"}).status_code == 409
    assert client.get(f"/api/sessions/{other}/{endpoint}", params={"workspace_id": alpha}).status_code == 409
    assert client.get(f"/api/sessions/s-alpha-assistant/{endpoint}", headers={"Authorization": f"Bearer {credential}"}).status_code == 200
    assert client.get(f"/api/sessions/{other}/{endpoint}").status_code == 200
    assert projects.session_home(identity) is None


@pytest.mark.parametrize("method,endpoint", [
    ("POST", "input"), ("POST", "exit"), ("POST", "wind-down"), ("POST", "tty-ticket"),
    ("POST", "ask/000001/answer"), ("POST", "comms"), ("POST", "threads"),
    ("POST", "references"), ("DELETE", "threads/fixture"), ("DELETE", "references/fixture"),
    ("GET", "comms"), ("GET", "threads"), ("GET", "references"), ("GET", "uploads/private-input.txt"),
])
def test_unassigned_controls_and_other_reads_still_require_project_authority(recovery, method, endpoint):
    client, identity, alpha, _other, credential, _connector = recovery
    path = f"/api/sessions/{identity}/{endpoint}"
    refused = client.request(method, path, json={})
    assert refused.status_code == 400 and refused.json()["detail"]["code"] == "project_required", refused.text
    assert client.request(method, path, json={"workspace_id": alpha}).status_code == 409
    assistant_body = {"text": "Denied recovery control", "ref": "recovery-control"} if endpoint == "comms" else {}
    denied = client.request(method, path, json=assistant_body, headers={"Authorization": f"Bearer {credential}"})
    assert denied.status_code == (403 if method == "POST" and endpoint == "comms" else 409), denied.text
    assert projects.session_home(identity) is None


@pytest.mark.parametrize("method", ["HEAD", "POST", "PUT", "PATCH", "DELETE"])
def test_retained_read_routes_do_not_add_http_methods(recovery, method):
    client, identity, *_ = recovery
    for endpoint in ("messages", "log", "ask"):
        assert client.request(method, f"/api/sessions/{identity}/{endpoint}").status_code == 405


def test_recovery_read_rechecks_ownership_before_the_reader_runs(recovery, monkeypatch):
    client, identity, alpha, *_ = recovery
    original = project_api.recheck
    calls = 0

    def reassigned():
        nonlocal calls
        calls += 1
        if calls == 2:
            projects.bind_session(identity, alpha)
        original()

    monkeypatch.setattr(project_api, "recheck", reassigned)
    response = client.get(f"/api/sessions/{identity}/messages")
    assert calls == 2
    assert response.status_code == 409 and response.json()["detail"]["code"] == "project_changed"
    assert "Retained history" not in response.text
