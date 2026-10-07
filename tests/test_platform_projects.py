"""Workspace lifecycle against isolated stores and actual bounded child processes."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
import os
import signal

import pytest

from lmer_platform import assistant, assistant_history, config, memory, projects
from lmer_platform import project_context as scope, registry, runs, spawn, store, uploads
from tests.conftest import strip_lmer_env


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    strip_lmer_env(monkeypatch)
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    monkeypatch.setenv(config.ENV_CONTAINER_URL, "http://platform.test:8600")
    executable = tmp_path / "lmer"
    executable.write_text("#!/bin/sh\nexec sleep 120\n")
    executable.chmod(0o755)
    settings = config.load({"lmer_bin": str(executable), "max_concurrent_sessions": 1})
    config.ensure_secret(settings)
    try:
        yield settings
    finally:
        for entry in registry.list_sessions(live_only=True):
            if entry["pid"] != os.getpid():
                with suppress(ProcessLookupError):
                    os.killpg(entry["pid"], signal.SIGTERM)
        for event in store.read_events():
            if event["type"] == "session_spawned":
                assert spawn.wait_for_exit_recorded(event["data"]["session"])


def test_migration_preserves_incumbent_mounts_history_and_assignments(fleet):
    legacy = assistant.start(fleet)
    assistant.set_instructions("General orders")
    assistant.set_handoff("General decision")
    memory.memory_dir().mkdir(exist_ok=True)
    (memory.memory_dir() / "MEMORY.md").write_text("General memory")
    runs.track("forge.test", "same/repository", "old-run")
    original = registry.read_session(legacy.session_id)
    old_memory, old_uploads = memory.memory_dir(), uploads.assistant_upload_dir()
    projects.migrate()
    projects.update("general", {"color": "Rose"})
    projects.migrate()
    assert len(projects.listing()) == 1
    assert projects.get("general")["color"] == "Rose"
    assert registry.read_session(legacy.session_id) == original
    assert assistant.status().session_id == legacy.session_id
    assert assistant.read_state().instructions == "General orders"
    assert assistant_history.latest()["session"] == legacy.session_id
    assert memory.memory_dir() == old_memory
    assert uploads.assistant_upload_dir() == old_uploads
    assert projects.run_home("forge.test/same/repository/old-run")["project_id"] == "general"


def test_independent_context_and_rotation_preserve_other_project(fleet):
    projects.migrate()
    alpha, beta = projects.create("Alpha"), projects.create("Beta")
    projects.reconcile(fleet)
    before = projects.status(beta["id"])
    with scope.bound(alpha["id"]):
        first = assistant.status().session_id
        assistant.set_handoff("Alpha-only decision")
        assistant.set_instructions("Alpha-only orders")
        assistant.notify("Alpha question")
        assistant_history.retain(first, {"id": "one", "kind": "message", "text": "Alpha conversation",
                                        "source": {"channel": "agent"}})
        alpha_memory = memory.memory_dir()
    with scope.bound(beta["id"]):
        assert assistant.read_state().handoff is None
        assert assistant.read_state().pending == ()
        assert memory.memory_dir() != alpha_memory
        assert assistant_history.latest()["session"] == before["session_id"]
    rotated = projects.rotate(alpha["id"], fleet)
    assert rotated["generation"] == 2
    assert rotated["handoff"] == "Alpha-only decision"
    assert rotated["pending"] == 1
    assert projects.status(beta["id"])["session_id"] == before["session_id"]
    page = assistant_history.page(first, since=0)
    assert any(row["text"] == "Alpha conversation" for row in page["messages"])
    with scope.bound(alpha["id"]):
        assert assistant_history.latest()["previous_session"] == first


def test_fifo_cancel_lowered_cap_and_restart_do_not_displace_workers(fleet):
    general = assistant.start(fleet)
    projects.migrate()
    worker = spawn.spawn_session(fleet, spawn.SpawnRequest("chat", "worker", no_repo=True, workspace_id="general"))
    rows = [projects.create(f"Project {number}") for number in range(5)]
    projects.reconcile(fleet)
    assert sum(projects.status(row["id"])["running"] for row in rows) == 3
    assert projects.status(rows[3]["id"])["queue_position"] == 1
    assert projects.status(rows[4]["id"])["queue_position"] == 2
    projects.stop(rows[3]["id"])
    config.update_stored({"max_live_assistants": 2})
    projects.stop(rows[0]["id"])
    projects.reconcile(fleet)
    assert not projects.status(rows[4]["id"])["running"]
    config.update_stored({"max_live_assistants": 4})
    projects.migrate()
    projects.reconcile(fleet)
    assert projects.status(rows[4]["id"])["running"]
    assert projects.status(rows[3]["id"])["desired"] == "stopped"
    assert registry.read_session(worker.session_id)
    assert projects.status("general")["session_id"] == general.session_id
    assert spawn._live_worker_count() == 1
    with pytest.raises(spawn.CapacityError):
        spawn.spawn_session(fleet, spawn.SpawnRequest("chat", "second", no_repo=True, workspace_id="general"))


def test_duplicate_names_and_concurrent_reconciliation_admit_once(fleet):
    projects.migrate()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(projects.create, name) for name in ("Alpha", "alpha")]
        results = []
        for future in futures:
            try:
                results.append(future.result())
            except projects.ProjectError as exc:
                assert exc.code == "name_taken"
    assert len(results) == 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: projects.reconcile(fleet), range(2)))
    assert len(registry.list_sessions(live_only=True)) == 1
    current = projects.status(results[0]["id"])
    assert current["generation"] == 1
    projects.update(results[0]["id"], {"name": "Renamed"})
    assert projects.status(results[0]["id"])["session_id"] == current["session_id"]


def test_failed_start_preserves_project_for_retry_and_stop_survives_restart(fleet, monkeypatch):
    projects.migrate()
    project = projects.create("Retry")
    real_start = assistant.start
    def fail(_config):
        raise assistant.AssistantError("launch unavailable")
    monkeypatch.setattr(assistant, "start", fail)
    projects.reconcile(fleet)
    assert projects.status(project["id"])["error"] == "launch unavailable"
    assert projects.status(project["id"])["lifecycle"] == "failed"
    monkeypatch.setattr(assistant, "start", real_start)
    projects.request_start(project["id"])
    projects.reconcile(fleet)
    assert projects.status(project["id"])["running"]
    projects.stop(project["id"])
    projects.migrate()
    projects.reconcile(fleet)
    assert not projects.status(project["id"])["running"]
