"""Assistant capacity beside eight workers, using isolated sleeping processes."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
import os
import signal

import pytest

from lmer_platform import assistant, comms, registry, spawn, store, transcripts
from lmer_platform import config as cfg
from tests.test_platform_assistant_routes import (  # noqa: F401
    SECRET, _clean_lmer_env, platform_root, bearer_header, client_for,
)


@pytest.fixture
def capacity_client(platform_root, tmp_path, monkeypatch):
    script = tmp_path / "fake-lmer"
    script.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv(cfg.ENV_CONTAINER_URL, "http://platform.test:8600")
    monkeypatch.setattr(transcripts, "TRANSCRIPT_ROOT", str(tmp_path / "transcripts"))
    config = cfg.load({"lmer_bin": str(script), "max_concurrent_sessions": 8})
    cfg.ensure_secret(config)
    try:
        yield client_for(config)
    finally:
        for entry in registry.list_sessions(live_only=True):
            with suppress(ProcessLookupError):
                os.kill(entry["pid"], signal.SIGTERM)
        for event in store.read_events():
            if event["type"] == "session_spawned":
                assert spawn.wait_for_exit_recorded(event["data"]["session"])


def workers():
    return {
        entry["id"]: entry for entry in registry.list_sessions(live_only=True)
        if entry["kind"] == registry.WORKER_KIND
    }


def fill_worker_pool(client):
    for number in range(8):
        response = client.post("/api/sessions", headers=bearer_header(), json={
            "taskdef": "chat", "target": f"capacity-worker-{number}",
        })
        assert response.status_code == 201, response.text
    entries = workers()
    assert len(entries) == 8
    assert_worker_cap(client, entries)
    return entries


def assert_worker_cap(client, entries):
    assert spawn._live_worker_count() == 8
    for taskdef in ("chat", "orchestrate"):
        response = client.post("/api/sessions", headers=bearer_header(), json={
            "taskdef": taskdef, "target": "ninth-worker", "kind": "assistant",
        })
        assert response.status_code == 429, response.text
        assert "8/8 worker sessions" in response.json()["detail"]
    assert workers() == entries


def assert_one_assistant(session):
    live = registry.list_sessions(live_only=True)
    assert len(live) == 9
    assert [entry["id"] for entry in live if entry["kind"] == registry.ASSISTANT_KIND] == [session]


def test_start_at_eight_workers_uses_reserved_capacity(capacity_client):
    client = capacity_client
    entries = fill_worker_pool(client)
    response = client.post("/api/assistant/start", headers=bearer_header())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["running"] is True
    assert body["generation"] == 1
    assert_one_assistant(body["session_id"])
    before = assistant.read_state()
    assert client.post("/api/assistant/start", headers=bearer_header()).status_code == 409
    assert assistant.read_state() == before
    assert_worker_cap(client, entries)


@pytest.mark.parametrize("caller", ["operator", "assistant"])
def test_rotation_at_eight_workers_preserves_continuity(capacity_client, caller):
    client = capacity_client
    response = client.post("/api/assistant/start", headers=bearer_header())
    assert response.status_code == 200, response.text
    previous = response.json()
    credential = cfg.active_assistant_credential()
    assert credential
    entries = fill_worker_pool(client)
    for seq, (channel, text) in enumerate((
        ("platform", "Keep the eight workers running"),
        ("agent", "The review is ready"),
    ), 1):
        assert comms.ensure_hub()._ingest(previous["session_id"], {
            "id": f"{previous['session_id']}/{seq}", "seq": seq,
            "kind": "message", "text": text, "at": "2026-09-23T07:00:00Z",
            "source": {"channel": channel},
        })
    assistant.notify("Worker three needs a decision")
    pending = assistant.read_state().pending
    headers = bearer_header(credential if caller == "assistant" else SECRET)
    handoff = "Eight workers are running; review worker three next"
    if caller == "assistant":
        saved = client.post("/api/assistant/handoff", headers=headers, json={"handoff": handoff})
        assert saved.status_code == 200, saved.text
    response = client.post("/api/assistant/rotate", headers=headers,
                           json={} if caller == "assistant" else {"handoff": handoff})
    assert response.status_code == 200, response.text
    current = response.json()
    assert current["running"] is True
    assert current["session_id"] != previous["session_id"]
    assert current["generation"] == previous["generation"] + 1
    assert current["handoff"] == handoff
    assert current["pending"] == 1
    assert assistant.read_state().pending == pending
    assert registry.read_session(previous["session_id"]) is None
    assert_one_assistant(current["session_id"])
    assert_worker_cap(client, entries)

    replacement_credential = cfg.active_assistant_credential()
    assert replacement_credential and replacement_credential != credential
    assert client.get("/api/assistant", headers=bearer_header(credential)).status_code == 401
    inherited = client.get("/api/assistant/handoff", headers=bearer_header(replacement_credential)).json()
    assert inherited["handoff"] == handoff
    assert inherited["previous_session"]["session"] == previous["session_id"]
    retained = client.get(inherited["previous_session"]["messages_url"],
                          headers=bearer_header(replacement_credential), params={"since": 0}).json()
    assert [row["text"] for row in retained["messages"] if row["role"] != "platform"] == [
        "Keep the eight workers running", "The review is ready",
    ]
    history = client.get(f"/api/assistant/history/{current['session_id']}",
                         headers=bearer_header(), params={"since": 0}).json()
    assert history["history"]["previous_session"] == previous["session_id"]
    assert sum(row["comms_kind"] == "rotation" for row in history["messages"]) == 1
    assert client.post("/api/assistant/start", headers=bearer_header()).status_code == 409
    assert_one_assistant(current["session_id"])


def test_concurrent_starts_at_capacity_publish_one_assistant(capacity_client):
    client = capacity_client
    entries = fill_worker_pool(client)

    def start():
        return client.post("/api/assistant/start", headers=bearer_header())

    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: start(), range(2)))
    assert sorted(reply.status_code for reply in replies) == [200, 409]
    current = next(reply.json() for reply in replies if reply.status_code == 200)
    assert current["generation"] == 1
    assert_one_assistant(current["session_id"])
    assert_worker_cap(client, entries)
