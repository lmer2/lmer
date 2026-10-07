"""Restart projections use durable assistant identity, not row order or run slug."""
from concurrent.futures import ThreadPoolExecutor
import os
import signal
import threading
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from lmer_platform import api, assistant, assistant_history, inventory, projects, registry, spawn
from lmer_platform import project_context as scope
from tests.test_platform_projects import fleet  # noqa: F401


@pytest.fixture
def fleet_view(fleet, monkeypatch):
    monkeypatch.setattr(api, "pull", lambda *a, **kw: SimpleNamespace(to_dict=lambda: {
        "present": True, "healthy": True,
    }))
    monkeypatch.setattr(inventory, "session_activity", lambda *a, **kw: None)
    projects.migrate()
    with TestClient(api.create_app(fleet, "fixture-operator"),
                    headers={"Authorization": "Bearer fixture-operator"}) as client:
        yield client


def stop_process(session):
    os.killpg(registry.read_session(session)["pid"], signal.SIGTERM)
    assert spawn.wait_for_exit_recorded(session)


def restart_project(settings, project_id):
    previous = projects.status(project_id)["session_id"]
    stop_process(previous)
    projects.reconcile(settings)
    with projects.database() as db:
        db.execute("UPDATE projects SET retry_at=0 WHERE id=?", (project_id,))
    projects.reconcile(settings)
    return previous, projects.status(project_id)["session_id"]


@pytest.mark.parametrize("reverse", [False, True])
def test_restart_projects_show_current_generations_and_retain_history(fleet_view, fleet, monkeypatch, reverse):
    alpha, beta = [projects.create(name) for name in ("Alpha", "Beta")]
    projects.request_start("general")
    projects.reconcile(fleet)
    before = projects.status(alpha["id"])["session_id"]
    assistant_history.retain(before, {"id": "decision", "kind": "message", "text": "Retained Alpha decision",
                                      "source": {"channel": "agent"}})
    old, current = restart_project(fleet, alpha["id"])
    assert old == before and old != current
    assert projects.status(alpha["id"])["generation"] == 2
    # A skewed clock and reversed registry ordering must not select generation 1.
    registry.update(old, started_at="2099-01-01T00:00:00Z")
    raw = registry.list_sessions(live_only=False)
    monkeypatch.setattr(api, "list_sessions", lambda **kw: list(reversed(raw)) if reverse else raw)
    expected = {project_id: projects.status(project_id)["session_id"]
                for project_id in ("general", alpha["id"], beta["id"])}
    for project_id in (None, *expected):
        response = fleet_view.get("/api/state", params={"workspace_id": project_id} if project_id else {})
        assert response.status_code == 200, response.text
        payload = response.json()
        rows = payload["runs"]
        assert {row["workspace_id"]: row["session"]["id"] for row in rows} == (
            {project_id: expected[project_id]} if project_id else expected)
        assert len(rows) == (1 if project_id else 3)
        assert len({(row["host"], row["project"], row["slug"]) for row in rows}) == len(rows)
        assert all(row["state"] == "running" for row in rows)
        assert payload["attention"] == []
        assert payload["totals"] == {"runs": len(rows), "live": len(rows), "attention": 0}
    assert registry.read_session(old)["exited"]
    assert projects.session_home(old) == alpha["id"]
    history = fleet_view.get(f"/api/assistant/history/{old}")
    assert history.status_code == 200, history.text
    assert any(row["text"] == "Retained Alpha decision" for row in history.json()["messages"])


@pytest.mark.parametrize("exhausted", [False, True])
def test_current_failure_remains_visible_after_a_recovered_generation(fleet_view, fleet, exhausted):
    project = projects.create("Alpha")
    projects.reconcile(fleet)
    old, current = restart_project(fleet, project["id"])
    if exhausted:
        _, current = restart_project(fleet, project["id"])
    stop_process(current)
    observed = projects.status(project["id"])
    assert observed["lifecycle"] == "failed"
    assert observed["error"] == "Assistant exited unexpectedly."
    projects.reconcile(fleet)
    assert projects.status(project["id"])["lifecycle"] == ("failed" if exhausted else "waiting")
    payload = fleet_view.get("/api/state", params={"workspace_id": project["id"]}).json()
    assert [row["session"]["id"] for row in payload["runs"]] == [current]
    assert payload["runs"][0]["state"] == "crashed"
    assert [row["session"]["id"] for row in payload["attention"]] == [current]
    assert not projects.status(project["id"])["running"]
    assert projects.status(project["id"])["error"] == "Assistant exited unexpectedly."
    assert registry.read_session(old)


def test_intentional_stop_does_not_resurrect_a_retired_crash(fleet_view, fleet):
    project = projects.create("Alpha")
    projects.reconcile(fleet)
    old, current = restart_project(fleet, project["id"])
    projects.stop(project["id"])
    supervisor = projects.Supervisor(fleet)
    try:
        supervisor.first_start()
        assert projects.status(project["id"])["lifecycle"] == "stopped"
        assert projects.status(project["id"])["session_id"] is None
        assert fleet_view.get("/api/state").json()["runs"] == []
        assert registry.read_session(old)
        assert assistant_history.generation(current)["previous_session"] == old
    finally:
        supervisor.stop()


def test_clean_supervisor_restart_adopts_survivor_without_changing_generation(fleet_view, fleet):
    project = projects.create("Alpha")
    first = projects.Supervisor(fleet)
    first.first_start()
    before = projects.status(project["id"])
    first.stop()
    second = projects.Supervisor(fleet)
    try:
        second.first_start()
        after = projects.status(project["id"])
        assert (after["session_id"], after["generation"]) == (before["session_id"], before["generation"])
        assert after["lifecycle"] == "running"
        payload = fleet_view.get("/api/state").json()
        assert [row["session"]["id"] for row in payload["runs"]] == [before["session_id"]]
        assert payload["attention"] == []
    finally:
        second.stop()


def test_starting_replacement_does_not_project_previous_crash_as_current(fleet_view, fleet, monkeypatch):
    project = projects.create("Alpha")
    projects.reconcile(fleet)
    old = projects.status(project["id"])["session_id"]
    stop_process(old)
    projects.reconcile(fleet)
    with projects.database() as db:
        db.execute("UPDATE projects SET retry_at=0 WHERE id=?", (project["id"],))
    entered, release = threading.Event(), threading.Event()
    original = assistant.start

    def held_start(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(assistant, "start", held_start)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(projects.reconcile, fleet)
        try:
            assert entered.wait(5)
            response = fleet_view.get("/api/projects")
            assert response.status_code == 200, response.text
            status = next(row["assistant"] for row in response.json()["projects"] if row["id"] == project["id"])
            assert status["lifecycle"] == "starting" and not status["running"]
            payload = fleet_view.get("/api/state").json()
            assert payload["runs"] == []
            assert payload["attention"] == []
        finally:
            release.set()
        pending.result(timeout=10)
    payload = fleet_view.get("/api/state").json()
    assert len(payload["runs"]) == 1
    assert payload["runs"][0]["session"]["id"] != old
    assert payload["runs"][0]["state"] == "running"


def test_unattributed_crash_and_worker_failures_are_not_hidden(fleet_view):
    for name, kind in (("s-unowned-assistant", "assistant"), ("s-worker", "worker")):
        registry.register(name, kind=kind, pid=99999999)
    payload = fleet_view.get("/api/state").json()
    assert {row["session"]["id"] for row in payload["attention"]} == {"s-unowned-assistant", "s-worker"}


@pytest.mark.parametrize("membership", ["missing", "unrelated", "live-predecessor"])
def test_filter_requires_a_dead_predecessor_in_the_current_conversation(fleet_view, fleet, membership):
    project = projects.create("Alpha")
    projects.reconcile(fleet)
    current = projects.status(project["id"])["session_id"]
    if membership == "live-predecessor":
        other, current = restart_project(fleet, project["id"])
        snapshot = registry.read_session(other)
        snapshot["live"] = True
        assert snapshot in projects.inventory_sessions([snapshot])
    else:
        other = "s-other-assistant"
        registry.register(other, kind="assistant", pid=99999999)
        projects.bind_session(other, project["id"])
        if membership == "unrelated":
            with scope.bound(project["id"]):
                assistant_history.register(other, number=0, started_at=None)
        payload = fleet_view.get("/api/state").json()
        assert {row["session"]["id"] for row in payload["runs"]} == {other, current}
        assert [row["session"]["id"] for row in payload["attention"]] == [other]


def test_session_only_assistant_keys_do_not_collapse_retained_generations():
    sessions = [{"id": name, "kind": "assistant", "live": live,
                 "run": {"host": None, "project": None, "slug": "orchestrate-fleet"}}
                for name, live in (("s-previous", False), ("s-current", True))]
    result = inventory.build_inventory([], sessions, titles={}, activity={}).to_dict()
    assert {row["slug"] for row in result["runs"]} == {"s-previous", "s-current"}
    assert {row["label"] for row in result["runs"]} == {"orchestrate-fleet"}
    assert [row["session"]["id"] for row in result["attention"]] == ["s-previous"]
