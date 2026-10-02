"""Legacy registry migration through the actual inventory and HTTP read paths."""
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from lmer_platform import api, config, project_api, projects, registry, runs, store
from tests.conftest import node_binary, require_node_toolchain, strip_lmer_env


def test_unassigned_http_inventory_reaches_mounted_operator_views():
    node = node_binary()
    if not node:
        require_node_toolchain("Node is required for mounted recovery UI tests")
    result = subprocess.run(
        [node, "node_modules/vitest/vitest.mjs", "run", "tests/UnassignedRuns.test.js"],
        cwd=Path(__file__).resolve().parents[1] / "web", capture_output=True, text=True, timeout=120,
        env={**os.environ, "PROJECT_TEST_PYTHON": sys.executable},
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_unassigned_recovery_controls_run_in_the_mounted_detail_view():
    node = node_binary()
    if not node:
        require_node_toolchain("Node is required for mounted recovery UI tests")
    result = subprocess.run(
        [node, "node_modules/vitest/vitest.mjs", "run", "tests/UnassignedRecovery.test.js"],
        cwd=Path(__file__).resolve().parents[1] / "web", capture_output=True, text=True, timeout=120,
        env={**os.environ, "PROJECT_TEST_PYTHON": sys.executable},
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def legacy_platform(tmp_path, monkeypatch):
    strip_lmer_env(monkeypatch)
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    monkeypatch.setattr(api, "pull", lambda *a, **kw: SimpleNamespace(to_dict=lambda: {
        "present": True, "healthy": True, "path": str(tmp_path / "mirror"),
    }))
    monkeypatch.setattr(api, "resolve_run_dir", lambda *a, **kw: None)
    settings = config.load()
    app = api.create_app(settings, "fixture-operator")
    with TestClient(app, headers={"Authorization": "Bearer fixture-operator"},
                    raise_server_exceptions=False) as client:
        yield client, settings


@pytest.mark.parametrize("case", ["no-run", "untracked-run", "tracked-run", "dead"])
def test_legacy_inventory_survives_migration_without_inventing_ownership(legacy_platform, tmp_path, case):
    client, settings = legacy_platform
    identity = "s-legacy-inventory"
    run = {"host": "forge.test", "project": "fixture/repository", "slug": "old-worker"}
    registry.register(identity, pid=99999999 if case == "dead" else os.getpid(),
                      task={"taskdef": "chat", "target": "legacy"},
                      run=run if case.endswith("-run") and case != "no-run" else None,
                      log_path=str(tmp_path / "legacy.log"))
    if case == "tracked-run":
        runs.track(**run, session_id=identity)
    original = registry.session_path(identity).read_bytes()
    before = client.get("/api/state")
    assert before.status_code == 200, before.text
    assert before.json()["runs"][0]["session"]["id"] == identity

    projects.migrate()
    expected_owner = None if case == "dead" else "general"
    assert projects.session_home(identity) == expected_owner
    raw = api.build_state(settings)
    assert raw["runs"][0]["session"]["id"] == identity
    assert len(raw["attention"]) == (1 if case == "dead" else 0)
    assert bool(projects.run_home(runs.run_key(**run))) == (case == "tracked-run")
    responses = {
        "state": client.get("/api/state"),
        "scoped": client.get("/api/state", params={"workspace_id": "general"}),
        "rescan": client.post("/api/rescan"),
        "scoped-rescan": client.post("/api/rescan", json={"workspace_id": "general"}),
        "context": client.get("/api/project-context", params={
            "workspace_id": "general", "source_workspace_id": "general",
        }),
    }
    assert {name: response.status_code for name, response in responses.items()} == {
        name: 200 for name in responses
    }
    for name in ("state", "rescan"):
        payload = responses[name].json()
        assert len(payload["runs"]) == 1
        row = payload["runs"][0]
        assert row["session"]["id"] == identity
        assert row["workspace_id"] == expected_owner
        assert row["home_revision"] == (1 if case == "tracked-run" else None)
        assert len(payload["attention"]) == len(raw["attention"])
        if case == "dead":
            assert payload["attention"][0]["session"]["id"] == identity
            assert payload["attention"][0]["workspace_id"] is None
            assert "workspace_name" not in row
        else:
            assert row["workspace_name"] == "General"
    for name in ("scoped", "scoped-rescan", "context"):
        assert len(responses[name].json()["runs"]) == (0 if case == "dead" else 1)
    assert responses["scoped"].json()["attention"] == []
    assert responses["context"].json()["source"] == {"id": "general", "name": "General"}
    projects.migrate()
    assert projects.session_home(identity) == expected_owner
    assert registry.session_path(identity).read_bytes() == original


def test_inventory_preserves_named_session_owner_and_flat_shapes(legacy_platform, tmp_path):
    client, _settings = legacy_platform
    tracked = runs.track("forge.test", "fixture/repository", "tracked-home")
    projects.migrate()
    alpha = projects.create("Alpha")
    identity = "s-named-inventory"
    registry.register(identity, pid=os.getpid(), task={"taskdef": "chat", "target": "named"},
                      log_path=str(tmp_path / "named.log"))
    projects.bind_session(identity, alpha["id"])
    response = client.get("/api/state", params={"workspace_id": alpha["id"]})
    assert response.status_code == 200, response.text
    rows = response.json()["runs"]
    assert len(rows) == 1
    assert rows[0]["session"]["id"] == identity
    assert rows[0]["workspace_id"] == alpha["id"]
    assert rows[0]["workspace_name"] == "Alpha"
    general = client.get("/api/state", params={"workspace_id": "general"}).json()
    assert [row["slug"] for row in general["runs"]] == [tracked.slug]
    flat = project_api.annotate({"sessions": registry.list_sessions(live_only=False)})
    assert flat["sessions"][0]["id"] == identity
    assert flat["sessions"][0]["workspace_id"] == alpha["id"]
    variants = project_api.annotate({"runs": [{"session": identity}, {"session": None}],
                                     "attention": [{"session": {"id": identity}}]})
    assert [row["workspace_id"] for row in variants["runs"]] == [alpha["id"], None]
    assert variants["attention"][0]["workspace_id"] == alpha["id"]
    home_first = project_api.annotate({"runs": [{**tracked.to_dict(), "session": {"id": identity}}]})
    assert home_first["runs"][0]["workspace_id"] == "general"
    assert projects.session_home(identity) == alpha["id"]
    with pytest.raises(registry.RegistryError, match="invalid session id"):
        project_api.annotate({"sessions": [{"id": "../escape"}]})
