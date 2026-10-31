"""Workspace lifecycle against isolated stores and actual bounded child processes."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
import json
import os
import signal
from typing import Optional

import pytest

from lmer_platform import assistant, assistant_history, config, memory, projects
from lmer_platform import project_api, project_events
from lmer_platform import project_context as scope, registry, runs, spawn, store, uploads
from tests.conftest import strip_lmer_env
from tests.timing_budgets import SETTLE, outlast


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    strip_lmer_env(monkeypatch)
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    monkeypatch.setenv(config.ENV_CONTAINER_URL, "http://platform.test:8600")
    executable = tmp_path / "lmer"
    # Must outlive the test, which the teardown below ends with a killpg: a
    # 120 s session exited under a loaded gate and an upload to it got 410 (#636).
    executable.write_text(f"#!/bin/sh\nexec sleep {outlast(SETTLE)}\n")
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


# --- run identity (#536 §6.1): the project paths take the key segment ----------

HOST, REPO, SLUG, UID = "forge.test", "team/repository", "chat-worker", "a" * 32
SLUG_KEY = runs.run_key(HOST, REPO, SLUG)
UID_KEY = runs.run_key(HOST, REPO, UID)


@pytest.fixture
def platform(tmp_path, monkeypatch):
    strip_lmer_env(monkeypatch)
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    projects.migrate()
    return projects.create("Alpha")["id"]


@dataclass(frozen=True)
class _UidRequest:
    host: str
    project: str
    slug: str
    uid: Optional[str] = None
    workspace_id: Optional[str] = None
    home_revision: Optional[int] = None
    confirm_project: bool = False

    def validate(self):
        return self


@dataclass(frozen=True)
class _SlugRequest:
    """The request shape #542 shipped with: no uid field at all."""

    host: str
    project: str
    slug: str
    workspace_id: Optional[str] = None
    home_revision: Optional[int] = None
    confirm_project: bool = False

    def validate(self):
        return self


def _reconfirm(key):
    with projects.database() as db:
        db.execute("UPDATE homes SET reconfirm=1 WHERE run_key=?", (key,))


def _seen_scope(request):
    seen = []

    @projects.run_operation
    def operate(_config, _request):
        seen.append(scope.current())
        return "done"

    assert operate(None, request) == "done"
    return seen


def _owners():
    with projects.database() as db:
        return sorted(tuple(row) for row in db.execute(
            "SELECT thread,owner,project_id,run_key FROM thread_owners"))


def test_run_operation_on_a_uid_request_reads_and_writes_the_uid_home(platform):
    projects.bind_run(UID_KEY, platform)
    projects.bind_run(SLUG_KEY, "general")
    _reconfirm(UID_KEY)
    _reconfirm(SLUG_KEY)
    with pytest.raises(projects.ProjectError) as refused:
        _seen_scope(_UidRequest(HOST, REPO, SLUG, uid=UID))
    assert refused.value.code == "project_reconfirmation_required"
    seen = _seen_scope(_UidRequest(HOST, REPO, SLUG, uid=UID, home_revision=1, confirm_project=True))
    assert seen == [platform]
    assert projects.run_home(UID_KEY)["reconfirm"] == 0
    assert projects.run_home(SLUG_KEY)["reconfirm"] == 1


def test_run_operation_on_a_slug_request_is_unchanged(platform):
    projects.bind_run(UID_KEY, platform)
    projects.bind_run(SLUG_KEY, "general")
    _reconfirm(UID_KEY)
    _reconfirm(SLUG_KEY)
    for request in (_SlugRequest(HOST, REPO, SLUG, home_revision=1, confirm_project=True),
                    _UidRequest(HOST, REPO, SLUG, home_revision=1, confirm_project=True)):
        _reconfirm(SLUG_KEY)
        assert _seen_scope(request) == ["general"]
        assert projects.run_home(SLUG_KEY)["reconfirm"] == 0
    assert projects.run_home(UID_KEY)["reconfirm"] == 1


def test_body_and_event_keys_bind_a_uid_run_to_its_uid_home(platform):
    projects.bind_run(UID_KEY, platform)
    projects.bind_run(SLUG_KEY, "general")
    assert project_api._run({"host": HOST, "project": REPO, "slug": SLUG, "uid": UID}) == UID_KEY
    assert project_api._run({"host": HOST, "project": REPO, "slug": SLUG}) == SLUG_KEY
    assert project_api._run({"host": HOST, "project": REPO}) is None
    assert projects.destination({"host": HOST, "project": REPO, "slug": SLUG, "uid": UID}) == platform
    project_events.enqueue("s-uid", {"id": "m-1", "kind": "message", "text": "hello",
                                     "source": {"channel": "agent"}},
                           run={"host": HOST, "project": REPO, "slug": SLUG, "uid": UID})
    project_events.enqueue("s-slug", {"id": "m-2", "kind": "message", "text": "hello",
                                      "source": {"channel": "agent"}},
                           run={"host": HOST, "project": REPO, "slug": SLUG, "uid": None})
    with projects.database() as db:
        rows = {row["session"]: (row["project_id"], json.loads(row["payload"])["data"])
                for row in db.execute("SELECT * FROM event_queue")}
    assert rows["s-uid"][0] == platform
    assert rows["s-uid"][1]["run_key"] == UID_KEY
    assert rows["s-slug"][0] == "general"
    # A schema-1 event's payload is the shape it always was: no uid field.
    assert rows["s-slug"][1] == {"session": "s-slug", "host": HOST, "project": REPO,
                                 "slug": SLUG, "run_key": SLUG_KEY}


def test_rekey_moves_home_sessions_and_thread_owners_and_is_idempotent(platform):
    projects.bind_run(SLUG_KEY, platform)
    projects.remember_launch(SLUG_KEY, "slot-a", None)
    _reconfirm(SLUG_KEY)
    projects.bind_session("s-old", platform, SLUG_KEY)
    projects.bind_session("s-other", platform, "forge.test/team/other")
    with projects.database() as db:
        db.execute("INSERT INTO thread_owners VALUES (?,?,?,?)", ("t-1", f"run:{SLUG_KEY}", platform, SLUG_KEY))
        db.execute("INSERT INTO thread_owners VALUES (?,?,?,NULL)", ("t-1", f"project:{platform}", platform))
    before = projects.run_home(SLUG_KEY)

    assert projects.rekey(SLUG_KEY, UID_KEY) == {"moved": True, "conflict": False}
    assert projects.run_home(SLUG_KEY) is None
    assert projects.run_home(UID_KEY) == {**before, "run_key": UID_KEY}
    assert projects.session_run("s-old") == UID_KEY
    assert projects.session_run("s-other") == "forge.test/team/other"
    after = _owners()
    assert after == sorted([("t-1", f"run:{UID_KEY}", platform, UID_KEY),
                            ("t-1", f"project:{platform}", platform, None)])

    assert projects.rekey(SLUG_KEY, UID_KEY) == {"moved": False, "conflict": False}
    assert projects.run_home(UID_KEY) == {**before, "run_key": UID_KEY}
    assert projects.session_run("s-old") == UID_KEY
    assert _owners() == after


def test_rekey_carries_what_the_new_key_lacks_and_equal_values_are_a_no_op(platform):
    projects.bind_run(SLUG_KEY, platform)
    projects.remember_launch(SLUG_KEY, "slot-a", None)
    projects.bind_run(UID_KEY, platform)
    with projects.database() as db:
        db.execute("INSERT INTO thread_owners VALUES (?,?,?,?)", ("t-1", f"run:{SLUG_KEY}", platform, SLUG_KEY))
        db.execute("INSERT INTO thread_owners VALUES (?,?,?,?)", ("t-1", f"run:{UID_KEY}", platform, UID_KEY))

    assert projects.rekey(SLUG_KEY, UID_KEY) == {"moved": True, "conflict": False}
    assert projects.run_home(SLUG_KEY) is None
    assert json.loads(projects.run_home(UID_KEY)["launch"]) == {"slot": "slot-a", "preset": None}
    assert _owners() == [("t-1", f"run:{UID_KEY}", platform, UID_KEY)]


def test_rekey_keeps_both_differing_homes_and_reports_the_conflict(platform):
    projects.bind_run(SLUG_KEY, platform)
    projects.bind_run(UID_KEY, "general")
    projects.remember_launch(SLUG_KEY, "slot-a", None)
    projects.bind_session("s-old", platform, SLUG_KEY)
    with projects.database() as db:
        db.execute("INSERT INTO thread_owners VALUES (?,?,?,?)", ("t-1", f"run:{SLUG_KEY}", platform, SLUG_KEY))

    assert projects.rekey(SLUG_KEY, UID_KEY) == {"moved": True, "conflict": True}
    # Neither home is dropped: the old key keeps its project, its sessions and its
    # thread owners; the new key keeps its own project and gains only what it lacked.
    assert projects.run_home(SLUG_KEY)["project_id"] == platform
    assert projects.run_home(UID_KEY)["project_id"] == "general"
    assert json.loads(projects.run_home(UID_KEY)["launch"]) == {"slot": "slot-a", "preset": None}
    assert projects.session_run("s-old") == SLUG_KEY
    assert _owners() == [("t-1", f"run:{SLUG_KEY}", platform, SLUG_KEY)]

    assert projects.rekey(SLUG_KEY, UID_KEY) == {"moved": False, "conflict": True}
    assert projects.run_home(SLUG_KEY)["project_id"] == platform
    assert projects.run_home(UID_KEY)["project_id"] == "general"


def test_rekey_without_the_project_store_is_a_no_op(tmp_path, monkeypatch):
    strip_lmer_env(monkeypatch)
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    assert projects.rekey(SLUG_KEY, UID_KEY) == {"moved": False, "conflict": False}
    assert not projects.path().exists()


def test_migration_reads_a_history_file_created_before_its_tables(fleet):
    """The history writer opens its file before it creates ``generations``
    (``assistant_history._database``), so a migration that lands in between
    finds the file with no table yet. That holds no session to carry — it is
    not a failed migration."""
    history = store.platform_dir() / "assistant-history.sqlite3"
    history.parent.mkdir(parents=True, exist_ok=True)
    history.touch()

    projects.migrate()

    assert projects.get("general")["id"] == "general"
