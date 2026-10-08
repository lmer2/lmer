"""Ownership checks at HTTP, CLI and child-process admission boundaries."""
from concurrent.futures import ThreadPoolExecutor
import base64
import json
import os
import signal
import subprocess
from threading import Event
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from lmer_platform import api, assistant, config, connectors, projects, runs, spawn, store
from lmer_platform import project_context as scope
from tests.test_platform_projects import fleet  # noqa: F401
from lmer_platform import registry, comms, transcripts, ctl, daemon, assistant_history, uploads
from tests.test_platform_comms import BusPlane, live_session, agent_posts
from tests.test_platform_ctl import Local
from tests.conftest import strip_lmer_env


@pytest.fixture
def client(fleet):
    projects.migrate()
    app = api.create_app(fleet, config.ensure_secret(fleet), state_builder=lambda *_a, **_k: {
        "runs": [row.to_dict() for row in runs.list_tracked()], "sessions": [], "attention": [],
    })
    with TestClient(app, headers={"Authorization": f"Bearer {config.ensure_secret(fleet)}"}) as client:
        yield client


def test_api_requires_destination_and_keeps_repository_separate(client):
    assert client.post("/api/sessions", json={"taskdef": "chat", "target": "new"}).status_code == 400
    alpha = client.post("/api/projects", json={"name": "Alpha"}).json()
    beta = client.post("/api/projects", json={"name": "Beta"}).json()
    payload = {"host": "forge.test", "project": "same/repository", "slug": "work"}
    result = client.post("/api/runs/adopt", json={**payload, "workspace_id": alpha["id"]})
    assert result.status_code == 200, result.text
    home = projects.run_home("forge.test/same/repository/work")
    assert home["project_id"] == alpha["id"]
    assert client.post("/api/runs/adopt", json={**payload, "workspace_id": beta["id"]}).status_code == 409
    assert client.get("/api/state", params={"workspace_id": beta["id"]}).json()["runs"] == []
    row = client.get("/api/state").json()["runs"][0]
    assert row["workspace_id"] == alpha["id"]
    assert row["project"] == "same/repository"


def test_assistant_credentials_scope_history_context_and_run_actions(client, fleet):
    alpha, beta = [client.post("/api/projects", json={"name": name}).json() for name in ("Alpha", "Beta")]
    with scope.bound(alpha["id"]):
        token = config.active_assistant_credential()
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/assistant", headers=headers).json()["workspace_id"] == alpha["id"]
    assert client.post("/api/assistant/instructions", headers=headers, json={"instructions": "Alpha orders"}).status_code == 200
    conflict = client.get("/api/assistant", headers=headers, params={"workspace_id": beta["id"]})
    assert conflict.status_code == 409
    denied = client.get(f"/api/assistant/history/{beta['assistant']['session_id']}", headers=headers)
    assert denied.status_code == 409
    assert client.post("/api/projects", headers=headers, json={"name": "Forbidden"}).status_code == 403
    with scope.bound(beta["id"]):
        assert assistant.read_state().instructions is None
    projects.rotate(alpha["id"], fleet)
    assert client.post("/api/assistant/handoff", headers=headers, json={"handoff": "stale"}).status_code == 401


def test_move_transfers_pending_once_and_rejects_stale_actions(client):
    alpha, beta = [client.post("/api/projects", json={"name": name}).json() for name in ("Alpha", "Beta")]
    data = {"host": "forge.test", "project": "same/repository", "slug": "work"}
    client.post("/api/runs/adopt", json={**data, "workspace_id": alpha["id"]})
    assistant.notify("Waiting question", kind="question", data=data)
    moved = client.post("/api/runs/move", json={**data, "workspace_id": alpha["id"],
                                              "home_revision": 1, "destination_id": beta["id"]})
    assert moved.status_code == 200, moved.text
    assert moved.json()["revision"] == 2
    assert client.post("/api/runs/answer", json={**data, "workspace_id": alpha["id"], "answer": "yes"}).status_code == 409
    result = client.post("/api/runs/answer", json={**data, "workspace_id": beta["id"], "answer": "yes"})
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "project_reconfirmation_required"
    with scope.bound(alpha["id"]):
        assert assistant.take_pending() == []
    with scope.bound(beta["id"]):
        pending = assistant.take_pending()
        assert sum(row["note"] == "Waiting question" for row in pending) == 1
        assert any(row["kind"] == "run_moved" for row in pending)
        assert assistant.take_pending() == []


def _track_unassigned(key, workspace_id):
    """A tracked run with no home record, as an upgraded fleet can carry."""
    with scope.bound(workspace_id):
        runs.track(*key)
    with projects.database() as db:
        db.execute("DELETE FROM homes WHERE run_key=?", (runs.run_key(*key),))


def test_unassigned_run_assignment_is_operator_only_and_revision_checked(client):
    alpha, beta = [client.post("/api/projects", json={"name": name}).json() for name in ("Alpha", "Beta")]
    key = ("forge.test", "repository", "orphan")
    _track_unassigned(key, alpha["id"])
    assert projects.run_home(runs.run_key(*key)) is None
    data = {"host": key[0], "project": key[1], "slug": key[2]}
    # A supplied source that disagrees with the destination is a stale tab, not
    # evidence of ownership, and it is refused before anything is written.
    conflict = client.post("/api/runs/move", json={**data, "workspace_id": beta["id"],
                                                   "destination_id": alpha["id"]})
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "project_changed"
    assert projects.run_home(runs.run_key(*key)) is None
    assigned = client.post("/api/runs/move", json={**data, "workspace_id": None,
                                                   "destination_id": alpha["id"]})
    assert assigned.status_code == 200, assigned.text
    home = projects.run_home(runs.run_key(*key))
    assert home["project_id"] == alpha["id"] and home["revision"] == 1
    stale = client.post("/api/runs/move", json={**data, "workspace_id": alpha["id"],
                                                "home_revision": 99, "destination_id": beta["id"]})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "project_changed"
    assert projects.run_home(runs.run_key(*key))["project_id"] == alpha["id"]


def test_assistants_cannot_assign_or_forget_unassigned_runs(client):
    alpha, beta = [client.post("/api/projects", json={"name": name}).json() for name in ("Alpha", "Beta")]
    key = ("forge.test", "repository", "assistant-target")
    _track_unassigned(key, alpha["id"])
    with scope.bound(alpha["id"]):
        token = config.active_assistant_credential()
    headers = {"Authorization": f"Bearer {token}"}
    data = {"host": key[0], "project": key[1], "slug": key[2]}
    moved = client.post("/api/runs/move", headers=headers,
                        json={**data, "destination_id": beta["id"]})
    assert moved.status_code == 409  # a different project is a scope conflict
    own = client.post("/api/runs/move", headers=headers,
                      json={**data, "destination_id": alpha["id"]})
    assert own.status_code == 403  # even its own project: assignment is operator-only
    assert projects.run_home(runs.run_key(*key)) is None
    forgotten = client.post("/api/runs/forget", headers=headers, json=data)
    assert forgotten.status_code == 403
    assert runs.get_tracked(*key) is not None


def test_unassigned_assignment_refuses_a_live_worker(client):
    alpha = client.post("/api/projects", json={"name": "Alpha"}).json()
    key = ("forge.test", "repository", "running")
    _track_unassigned(key, alpha["id"])
    registry.register("s-running", pid=os.getpid(), kind="worker",
                      run={"host": key[0], "project": key[1], "slug": key[2]})
    data = {"host": key[0], "project": key[1], "slug": key[2]}
    refused = client.post("/api/runs/move", json={**data, "destination_id": alpha["id"]})
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "run_active"
    assert projects.run_home(runs.run_key(*key)) is None


def test_move_out_of_an_archived_home_transfers_without_adopting(client):
    alpha, beta = [client.post("/api/projects", json={"name": name}).json() for name in ("Alpha", "Beta")]
    key = ("forge.test", "repository", "stranded")
    with scope.bound(alpha["id"]):
        runs.track(*key)
    with projects.database() as db:
        db.execute("UPDATE projects SET archived=1 WHERE id=?", (alpha["id"],))
    data = {"host": key[0], "project": key[1], "slug": key[2]}
    # The run keeps its recorded (archived) home; recovery is a transfer, so the
    # assignment path that would invent ownership is never reached.
    moved = client.post("/api/runs/move", json={**data, "workspace_id": alpha["id"],
                                                "home_revision": 1, "destination_id": beta["id"]})
    assert moved.status_code == 200, moved.text
    home = projects.run_home(runs.run_key(*key))
    assert home["project_id"] == beta["id"] and home["revision"] == 2


def test_operator_forgets_unassigned_run_without_a_project(client):
    alpha = client.post("/api/projects", json={"name": "Alpha"}).json()
    key = ("forge.test", "repository", "forgotten")
    _track_unassigned(key, alpha["id"])
    assert any(row["slug"] == "forgotten" for row in client.get("/api/state").json()["runs"])
    data = {"host": key[0], "project": key[1], "slug": key[2]}
    result = client.post("/api/runs/forget", json=data)
    assert result.status_code == 200, result.text
    assert result.json() == {"forgotten": True}
    assert runs.get_tracked(*key) is None
    # The row is gone on the next read; only the tracked-index entry was removed.
    assert all(row["slug"] != "forgotten" for row in client.get("/api/state").json()["runs"])


def test_forget_excludes_a_retained_dead_session_from_the_real_fleet_view(tmp_path, monkeypatch):
    """Forgetting a completed unassigned run removes its row even though the
    run's dead registry entry is retained as evidence (review round 1, major)."""
    strip_lmer_env(monkeypatch)
    mirror = tmp_path / "mirror"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    monkeypatch.setattr(api, "pull", lambda *a, **kw: SimpleNamespace(
        to_dict=lambda: {"present": True, "healthy": True, "path": str(mirror)}))
    settings = config.load({"work_repo_mirror": str(mirror)})
    secret = config.ensure_secret(settings)
    projects.migrate()
    alpha = projects.create("Alpha")
    host, project, slug = "forge.test", "fixture/repository", "completed-retained"
    key = runs.run_key(host, project, slug)
    dead = "s-completed-retained"
    with scope.bound(alpha["id"]):
        runs.track(host, project, slug, session_id=dead)
    # A clean-exit run: completed, no retained registry row. This is the positive
    # control that already disappeared before the fix and must keep doing so.
    clean_slug = "completed-clean"
    clean_key = runs.run_key(host, project, clean_slug)
    with scope.bound(alpha["id"]):
        runs.track(host, project, clean_slug)
    with projects.database() as db:
        db.execute("DELETE FROM homes WHERE run_key IN (?,?)", (key, clean_key))
    for name in (slug, clean_slug):
        run_dir = mirror / host / project / "runs" / name
        run_dir.mkdir(parents=True)
        (run_dir / "state.yaml").write_text(
            f"schema: 1\nslug: {name}\nstatus: complete\nstop_reason: complete\n")
    run_dir = mirror / host / project / "runs" / slug
    registry.register(dead, pid=99999999, run={"host": host, "project": project, "slug": slug})
    # An unrelated unassigned legacy session must survive the same refresh.
    registry.register("s-legacy-unassigned", pid=99999999,
                      run={"host": None, "project": None, "slug": "legacy"})
    app = api.create_app(settings, secret)
    with TestClient(app, headers={"Authorization": f"Bearer {secret}"}) as client:
        before = client.get("/api/state").json()["runs"]
        row = next(entry for entry in before if entry["slug"] == slug)
        assert row["state"] == "complete" and row["workspace_id"] is None
        assert any(entry["slug"] == clean_slug for entry in before)
        assert any(entry["slug"] == "legacy" for entry in before)
        evidence = (run_dir / "state.yaml").read_bytes()
        clean_forget = client.post("/api/runs/forget", json={"host": host, "project": project, "slug": clean_slug})
        assert clean_forget.status_code == 200, clean_forget.text
        forgotten = client.post("/api/runs/forget", json={"host": host, "project": project, "slug": slug})
        assert forgotten.status_code == 200, forgotten.text
        assert forgotten.json() == {"forgotten": True}
        after = client.get("/api/state").json()["runs"]
        assert all(entry["slug"] not in (slug, clean_slug) for entry in after)
        assert any(entry["slug"] == "legacy" for entry in after)
        # Evidence is retained, not deleted: the run dir and its dead entry survive.
        assert (run_dir / "state.yaml").read_bytes() == evidence
        assert registry.read_session(dead) is not None
        assert key in runs.forgotten_keys()


def test_run_move_waits_for_launch_and_then_refuses_live_worker(client, fleet, monkeypatch):
    alpha, beta = [projects.create(name) for name in ("Alpha", "Beta")]
    with scope.bound(alpha["id"]):
        runs.track("forge.test", "repository", "chat-task")
    entered, release, attempting = Event(), Event(), Event()
    real_spawn = spawn._spawn_session

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return real_spawn(*args, **kwargs)

    def move():
        attempting.set()
        return projects.move("forge.test/repository/chat-task", beta["id"], expected_project=alpha["id"])

    monkeypatch.setattr(spawn, "_spawn_session", paused)
    request = spawn.SpawnRequest("chat", "task", repo_url="https://forge.test/repository", workspace_id=alpha["id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        started = pool.submit(spawn.spawn_session, fleet, request)
        assert entered.wait(10)
        moved = pool.submit(move)
        assert attempting.wait(10)
        assert not moved.done()
        release.set()
        assert started.result().session_id
        with pytest.raises(projects.ProjectError, match="Stop the worker"):
            moved.result()


def test_low_level_assistant_spawn_cannot_bypass_durable_admission(client, fleet):
    with pytest.raises(spawn.SpawnError, match="admission queue"):
        spawn.spawn_session(fleet, spawn.SpawnRequest("orchestrate", "ul", no_repo=True, workspace_id="general"),
                            kind="assistant")


def test_explicit_context_and_relay_name_source_without_granting_control(client):
    alpha, beta = [client.post("/api/projects", json={"name": name}).json() for name in ("Alpha", "Beta")]
    data = {"host": "forge.test", "project": "same/repository", "slug": "beta-run"}
    assert client.post("/api/runs/adopt", json={**data, "workspace_id": beta["id"]}).status_code == 200
    with scope.bound(alpha["id"]):
        token = config.active_assistant_credential()
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/project-context", headers=headers, params={"source_workspace_id": beta["id"]}).status_code == 403
    excerpt = client.get("/api/project-context", params={"workspace_id": alpha["id"], "source_workspace_id": beta["id"]})
    assert excerpt.status_code == 200, excerpt.text
    assert excerpt.json()["source"] == {"id": beta["id"], "name": "Beta"}
    assert excerpt.json()["runs"][0]["slug"] == "beta-run"
    assert client.post("/api/runs/forget", headers=headers, json=data).status_code == 409
    assert client.post("/api/project-relay", headers=headers, json={"destination_id": beta["id"], "text": "Requested excerpt"}).status_code == 403
    relay = client.post("/api/project-relay", json={"workspace_id": alpha["id"], "destination_id": beta["id"], "text": "Requested excerpt"})
    assert relay.status_code == 200, relay.text
    with scope.bound(beta["id"]):
        pending = assistant.take_pending()
    assert pending[0]["data"]["source_name"] == "Alpha"
    assert pending[0]["note"] == "Explicit relay from Alpha: Requested excerpt"


def test_two_real_question_buses_keep_identical_ids_separate(client, fleet, tmp_path):
    alpha, beta = [projects.create(name) for name in ("Alpha", "Beta")]
    planes = [BusPlane(tmp_path, "s-project-a"), BusPlane(tmp_path, "s-project-b")]
    try:
        ids = []
        for project, plane, session in zip((alpha, beta), planes, ("s-project-a", "s-project-b")):
            with scope.bound(project["id"]):
                tracked = runs.track("forge.test", "repo", project["name"])
            live_session(plane, session, run={"host": tracked.host, "project": tracked.project, "slug": tracked.slug})
            projects.bind_session(session, project["id"], tracked.key)
            question = agent_posts(plane, "question", "Question one")
            ids.append(question.id)
        assert ids[0].rsplit("/", 1)[1] == ids[1].rsplit("/", 1)[1] == "000001"
        first = client.post(f"/api/sessions/s-project-a/ask/000001/answer",
                            json={"answer": "Alpha answer", "workspace_id": alpha["id"]})
        assert first.status_code == 200, first.text
        duplicate = client.post(f"/api/sessions/s-project-a/ask/000001/answer",
                                json={"answer": "Again", "workspace_id": alpha["id"]})
        assert duplicate.status_code == 409
        wrong = client.post(f"/api/sessions/s-project-b/ask/000001/answer",
                            json={"answer": "Wrong project", "workspace_id": alpha["id"]})
        assert wrong.status_code == 409
        assert planes[1].log.question(ids[1]).open
        assert not planes[0].log.question(ids[0]).open
    finally:
        hub = comms.get_hub()
        if hub:
            hub.stop()
        for plane in planes:
            plane.stop()


def test_scoped_inventory_has_no_other_project_metadata(fleet):
    projects.migrate()
    alpha, beta = [projects.create(name) for name in ("Alpha", "Beta")]
    rows = []
    for project in (alpha, beta):
        with scope.bound(project["id"]):
            tracked = runs.track("forge.test", "shared", project["name"])
        rows.append({**tracked.to_dict(), "state": "complete", "live": False})
    payload = {"runs": rows, "attention": rows, "tracked": {"runs": rows, "count": 2},
               "totals": {"runs": 2, "attention": 2, "live": 0}, "counts": {"complete": 2},
               "relations": [{"a": "forge.test/shared/Alpha", "b": "forge.test/shared/Beta"}]}
    app = api.create_app(fleet, config.ensure_secret(fleet), state_builder=lambda *_: payload)
    with TestClient(app, headers={"Authorization": f"Bearer {config.ensure_secret(fleet)}"}) as http:
        selected = http.get("/api/state", params={"workspace_id": alpha["id"]}).json()
        assert selected["tracked"]["count"] == selected["totals"]["runs"] == selected["totals"]["attention"] == 1
        assert selected["tracked"]["runs"][0]["slug"] == "Alpha"
        assert selected["relations"] == []
        assert selected["counts"] == {"complete": 1}
        assert http.get("/api/state").json()["tracked"]["count"] == 2
        assert http.post("/api/sessions", json={"workspace_id": [alpha["id"]]}).status_code == 400


def test_project_settings_colors_archive_and_restore_through_http(client, fleet):
    assert client.post("/api/projects", json={"name": " "}).status_code == 400
    alpha = client.post("/api/projects", json={"name": "Alpha"}).json()
    assert client.post("/api/projects", json={"name": " alpha "}).status_code == 409
    identifier = alpha["id"]
    initial = projects.status(identifier)["session_id"]
    for color in projects.COLORS:
        updated = client.patch(f"/api/projects/{identifier}", json={"color": color})
        assert updated.status_code == 200, updated.text
        assert updated.json()["id"] == identifier
        projects.migrate()
        assert projects.get(identifier)["color"] == color
    assert client.patch(f"/api/projects/{identifier}", json={"color": "Random"}).status_code == 400
    custom = client.patch(f"/api/projects/{identifier}", json={"color": "#FF69B4"})
    assert custom.status_code == 200, custom.text
    assert custom.json()["color"] == "#ff69b4"
    assert projects.get(identifier)["color"] == "#ff69b4"
    for malformed in ("#fff", "ff69b4", "#ff69b4ff", "#gg69b4", " #ff69b4", "#ff69b4\n", 0xff69b4, None):
        assert client.patch(f"/api/projects/{identifier}", json={"color": malformed}).status_code == 400, malformed
    assert projects.get(identifier)["color"] == "#ff69b4"
    renamed = client.patch(f"/api/projects/{identifier}", json={"name": "Renamed", "repositories": ["https://forge.test/repo"]})
    assert renamed.status_code == 200
    assert projects.status(identifier)["session_id"] == initial
    with scope.bound(identifier):
        assistant.set_instructions("Retained orders")
        assistant.set_handoff("Retained handover")
        assistant.notify("Pending decision")
    assert client.post(f"/api/projects/{identifier}/archive").status_code == 409
    with scope.bound(identifier):
        assistant.take_pending()
    assert client.post(f"/api/projects/{identifier}/archive").status_code == 200
    assert projects.get(identifier)["archived"]
    assert client.post("/api/assistant/start", json={"workspace_id": identifier}).status_code == 409
    restored = client.post(f"/api/projects/{identifier}/restore")
    assert restored.status_code == 200, restored.text
    projects.reconcile(fleet)
    assert not projects.status(identifier)["running"]
    assert projects.status(identifier)["desired"] == "stopped"
    with scope.bound(identifier):
        assert assistant.read_state().instructions == "Retained orders"
        assert assistant.read_state().handoff == "Retained handover"


def test_settings_inheritance_only_changes_next_generation(client, fleet):
    config.update_stored({"assistant_model": "default-one"})
    alpha, beta = [client.post("/api/projects", json={"name": name}).json() for name in ("Alpha", "Beta")]
    assert projects.status(alpha["id"])["settings"]["model"] == "default-one"
    config.update_stored({"assistant_model": "default-two"})
    result = client.patch(f"/api/projects/{alpha['id']}", json={"settings": {"model": "alpha-model"}})
    assert result.status_code == 200, result.text
    assert projects.status(alpha["id"])["settings"]["model"] == "default-one"
    effective = client.get("/api/assistant/config", params={"workspace_id": alpha["id"]}).json()
    assert effective["settings"]["model"]["value"] == "alpha-model"
    assert projects.rotate(alpha["id"], fleet)["settings"]["model"] == "alpha-model"
    assert projects.status(beta["id"])["settings"]["model"] == "default-one"
    assert projects.rotate(beta["id"], fleet)["settings"]["model"] == "default-two"


def test_credentials_are_scrubbed_in_every_project_context(client):
    alpha, beta = [client.post("/api/projects", json={"name": name}).json() for name in ("Alpha", "Beta")]
    tokens = []
    for project in (alpha, beta):
        with scope.bound(project["id"]):
            tokens.append(config.active_assistant_credential())
            environment = assistant.env_file_path().read_text()
            assert project["id"] in environment and project["name"] in environment
    for context in (None, alpha["id"], beta["id"]):
        with scope.bound(context):
            scrubbed = transcripts.scrub_credentials(" ".join(tokens))
            assert all(token not in scrubbed for token in tokens)


def test_fleet_worker_limit_edits_apply_to_admission_without_killing_occupants(client, fleet):
    assert client.post("/api/settings", json={"max_concurrent_sessions": 2, "max_live_assistants": 1}).status_code == 200
    assert client.get("/api/settings").json()["max_concurrent_sessions"] == 2
    projects.create("Queued")
    projects.reconcile(fleet)
    workers = [spawn.spawn_session(fleet, spawn.SpawnRequest("chat", f"worker-{n}", no_repo=True, workspace_id="general")) for n in range(2)]
    assert client.post("/api/settings", json={"max_concurrent_sessions": 1}).status_code == 200
    assert spawn._live_worker_count() == 2
    refused = client.post("/api/sessions", json={"taskdef": "chat", "target": "third", "no_repo": True, "workspace_id": "general"})
    assert refused.status_code == 429, refused.text
    assert projects.status(projects.listing()[1]["id"])["running"]
    assert len(workers) == 2


def test_ctl_binding_reaches_real_http_admission(client, fleet, monkeypatch, capsys):
    monkeypatch.setenv(ctl.ENV_PLATFORM_URL, "http://testserver")
    monkeypatch.setenv(ctl.ENV_PLATFORM_CREDENTIAL, config.ensure_secret(fleet))
    key = "forge.test/repository/adopted"
    assert ctl.main(["runs", "adopt", key], transport=Local(client)) == 1
    assert json.loads(capsys.readouterr().err)["status"] == 400
    alpha = projects.create("Alpha")
    assert ctl.main(["--workspace-id", alpha["id"], "runs", "adopt", key], transport=Local(client)) == 0
    capsys.readouterr()
    assert projects.run_home(key)["project_id"] == alpha["id"]
    assert ctl.main(["--workspace-id", "general", "runs", "forget", key], transport=Local(client)) == 1
    assert json.loads(capsys.readouterr().err)["status"] == 409
    assert daemon.main(["spawn", "chat", "unassigned"]) == 2
    assert "Choose a project" in capsys.readouterr().err


def test_archive_refuses_unfinished_runs_and_retains_slot_assignments(client, monkeypatch):
    monkeypatch.setattr(connectors, "_CONNECTORS", None)
    alpha = client.post("/api/projects", json={"name": "Alpha"}).json()
    payload = {"host": "forge.test", "project": "repo", "slug": "unfinished", "workspace_id": alpha["id"]}
    assert client.post("/api/runs/adopt", json=payload).status_code == 200
    assert client.post(f"/api/projects/{alpha['id']}/archive").status_code == 409
    assert client.post("/api/runs/forget", json=payload).status_code == 200
    config.update_stored({"slots": [{"name": "retained", "preset": "example", "applicable_projects": [alpha["id"]]}]})
    before = config.config_path().read_bytes()
    assert client.post(f"/api/projects/{alpha['id']}/archive").status_code == 200
    assert config.config_path().read_bytes() == before
    assert client.post(f"/api/projects/{alpha['id']}/restore").status_code == 200
    assert config.config_path().read_bytes() == before


def test_two_rotations_reopen_only_own_retained_history_and_handover(client, fleet):
    alpha, beta = [client.post("/api/projects", json={"name": name}).json() for name in ("Alpha", "Beta")]
    sessions = []
    for number in range(1, 4):
        if number > 1:
            response = client.post("/api/assistant/rotate", json={"workspace_id": alpha["id"], "handoff": "Alpha compact handover"})
            assert response.status_code == 200, response.text
        session = projects.status(alpha["id"])["session_id"]
        sessions.append(session)
        assistant_history.retain(session, {"id": "same-id", "kind": "message", "text": f"Alpha decision {number}",
                                           "at": f"2026-09-24T12:0{number}:00Z", "source": {"channel": "agent"}})
    projects.migrate()
    fresh_app = api.create_app(fleet, config.ensure_secret(fleet))
    with TestClient(fresh_app, headers=client.headers) as fresh:
        selected, pages = sessions[-1], []
        while selected:
            response = fresh.get(f"/api/assistant/history/{selected}", params={"workspace_id": alpha["id"], "limit": 10})
            assert response.status_code == 200, response.text
            page = response.json()
            pages.insert(0, page)
            selected = page["history"]["previous_session"]
        rows = [row for page in pages for row in page["messages"]]
        assert [row["text"] for row in rows if row["role"] != "platform"] == [f"Alpha decision {number}" for number in range(1, 4)]
        separators = [row for row in rows if row["comms_kind"] == "rotation"]
        assert len(separators) == 2
        assert all("does not have this conversation verbatim" in row["text"] for row in separators)
        assert len({row["id"] for row in rows}) == len(rows)
        assert fresh.get(f"/api/assistant/history/{sessions[0]}", params={"workspace_id": beta["id"]}).status_code == 409
        handoff = fresh.get("/api/assistant/handoff", params={"workspace_id": alpha["id"]}).json()
        assert handoff["handoff"] == "Alpha compact handover"
        assert "Alpha decision" not in str(handoff)
        missing = fresh.get(f"/api/assistant/history/{sessions[0]}", params={"workspace_id": alpha["id"], "before": "absent"})
        assert missing.status_code == 409
        assert "no longer available" in missing.json()["detail"]


def test_terminal_ticket_refuses_changed_project_and_exited_caller_generation(client, fleet):
    alpha, beta = [client.post("/api/projects", json={"name": name}).json() for name in ("Alpha", "Beta")]
    with scope.bound(alpha["id"]):
        run = runs.track("forge.test", "repo", "terminal")
        credential = config.active_assistant_credential()
    ended = subprocess.Popen(["true"])
    assert ended.wait() == 0
    registry.register("s-terminal", kind="worker", pid=ended.pid, run={"host": run.host, "project": run.project, "slug": run.slug})
    projects.bind_session("s-terminal", alpha["id"], run.key)
    ticket = client.post("/api/sessions/s-terminal/tty-ticket", json={"workspace_id": alpha["id"]}).json()["ticket"]
    projects.move(run.key, beta["id"], expected_project=alpha["id"])
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(f"/api/sessions/s-terminal/tty?ticket={ticket}"):
            pytest.fail("A moved session accepted its source project's ticket")
    assert refused.value.code == 1008
    projects.move(run.key, alpha["id"], expected_project=beta["id"])
    ticket = client.post("/api/sessions/s-terminal/tty-ticket", headers={"Authorization": f"Bearer {credential}"}).json()["ticket"]
    projects.rotate(alpha["id"], fleet)
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(f"/api/sessions/s-terminal/tty?ticket={ticket}"):
            pytest.fail("An exited assistant's ticket was accepted")
    assert refused.value.code == 1008


def test_same_named_uploads_keep_project_storage_and_survive_own_rotation(client, fleet):
    alpha, beta = [client.post("/api/projects", json={"name": name}).json() for name in ("Alpha", "Beta")]
    saved = []
    for project in (alpha, beta):
        session = project["assistant"]["session_id"]
        raw = b"\x89PNG\r\n\x1a\n" + project["name"].encode() + b"\0" * 64
        response = client.post(f"/api/sessions/{session}/uploads", json={
            "workspace_id": project["id"], "name": "decision.png", "data": base64.b64encode(raw).decode(),
        })
        assert response.status_code == 201, response.text
        saved.append((project, session, response.json()["name"], raw))
    with scope.bound(alpha["id"]):
        alpha_directory = uploads.assistant_upload_dir()
    with scope.bound(beta["id"]):
        assert uploads.assistant_upload_dir() != alpha_directory
    projects.rotate(alpha["id"], fleet)
    for project, session, name, raw in saved:
        path = f"/api/sessions/{session}/uploads/{name}"
        assert client.get(path, params={"workspace_id": project["id"]}).content == raw
        other = beta if project == alpha else alpha
        assert client.get(path, params={"workspace_id": other["id"]}).status_code == 409


def test_inflight_authority_from_crashed_assistant_cannot_start_worker_before_recovery(client, fleet):
    alpha = client.post("/api/projects", json={"name": "Alpha"}).json()
    session = alpha["assistant"]["session_id"]
    with scope.bound(alpha["id"]):
        credential = config.active_assistant_credential()
    os.killpg(registry.read_session(session)["pid"], signal.SIGKILL)
    assert spawn.wait_for_exit_recorded(session)
    response = client.post("/api/sessions", headers={"Authorization": f"Bearer {credential}"},
                           json={"taskdef": "chat", "target": "orphan action", "no_repo": True})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "assistant_generation_changed"
    assert spawn._live_worker_count() == 0
