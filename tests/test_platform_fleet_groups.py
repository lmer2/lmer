"""Fleet grouping through the existing operator settings and state routes."""
from pathlib import Path
import subprocess

from fastapi.testclient import TestClient
import pytest

from lmer_platform import api, config, projects, runs, store
from lmer_platform import project_context as scope
from tests.conftest import node_binary, require_node_toolchain
from tests.test_platform_projects import fleet  # noqa: F401


GROUPS = [{"name": "Team", "patterns": ["team/", "tools/team-"]}]


def inventory(settings):
    return {"config": api._config_summary(settings, {}), "sessions": [], "attention": [],
            "runs": [row.to_dict() for row in runs.list_tracked()]}


@pytest.fixture
def client(fleet):
    secret = config.ensure_secret(fleet)
    projects.migrate()
    app = api.create_app(fleet, secret, state_builder=inventory)
    with TestClient(app, headers={"Authorization": f"Bearer {secret}"}) as client:
        yield client


def test_missing_empty_and_restart_persistence_preserve_other_settings(client):
    assert client.get("/api/settings").json()["fleet_groups"] == []
    store.write_json(config.config_path(), {"max_live_assistants": 3, "future_option": {"keep": True}})
    supplied = [{"name": " Team ", "patterns": [" team/ ", "tools/team-"]}]
    saved = client.post("/api/settings", json={"fleet_groups": supplied})
    assert saved.status_code == 200, saved.text
    assert saved.json()["fleet_groups"] == GROUPS
    raw = store.read_json(config.config_path())
    assert raw["future_option"] == {"keep": True} and raw["max_live_assistants"] == 3
    restarted = config.load()
    assert restarted.fleet_groups == GROUPS
    with TestClient(api.create_app(restarted, config.ensure_secret(restarted), state_builder=inventory),
                    headers=client.headers) as again:
        assert again.get("/api/settings").json()["fleet_groups"] == GROUPS
    assert client.get("/api/state").json()["config"]["fleet_groups"] == GROUPS
    assert client.post("/api/settings", json={"max_live_assistants": 2}).status_code == 200
    assert config.load().fleet_groups == GROUPS
    assert client.post("/api/settings", json={"fleet_groups": []}).status_code == 200
    assert client.get("/api/state").json()["config"]["fleet_groups"] == []
    assert store.read_json(config.config_path())["future_option"] == {"keep": True}


@pytest.mark.parametrize("value", [
    None, "team", {}, [None], [{"name": "Team"}], [{"name": "Team", "patterns": "team/"}],
    [{"name": "Team", "patterns": []}], [{"name": " ", "patterns": ["team/"]}],
    [{"name": 3, "patterns": ["team/"]}], [{"name": "Team", "patterns": [False]}],
    [{"name": "Team", "patterns": [""]}], [{"name": "Team", "patterns": ["a\nb"]}],
    [{"name": "Team\x00", "patterns": ["team/"]}], [{"name": "x" * 65, "patterns": ["team/"]}],
    [{"name": "Team", "patterns": ["x" * 257]}], [{"name": "Team", "patterns": ["x"] * 17}],
    [{"name": str(i), "patterns": ["x"]} for i in range(33)],
    GROUPS + [{"name": " team ", "patterns": ["another/"]}],
    [{"name": "Team", "patterns": ["team/"], "regex": True}],
])
def test_invalid_groups_refuse_the_entire_patch(client, value):
    assert client.post("/api/settings", json={"fleet_groups": GROUPS, "max_live_assistants": 2}).status_code == 200
    before = config.config_path().read_bytes()
    reply = client.post("/api/settings", json={"fleet_groups": value, "max_live_assistants": 3})
    assert reply.status_code == 400, reply.text
    assert reply.json()["detail"]
    assert config.config_path().read_bytes() == before


def test_other_invalid_setting_does_not_partially_write_groups(client):
    before = store.read_json(config.config_path())
    for other in ({"max_live_assistants": -1}, {"unknown_setting": 1}):
        reply = client.post("/api/settings", json={"fleet_groups": GROUPS, **other})
        assert reply.status_code == 400, reply.text
        assert store.read_json(config.config_path()) == before


def test_order_edit_remove_and_literal_characters_round_trip(client):
    groups = GROUPS + [{"name": "Widget", "patterns": ["widget", "(a+)+$", "[", "*"]}]
    for value in (groups, groups[::-1], [dict(groups[0], name="Renamed")], []):
        reply = client.post("/api/settings", json={"fleet_groups": value})
        assert reply.status_code == 200, reply.text
        assert reply.json()["fleet_groups"] == value
        assert client.get("/api/state").json()["config"]["fleet_groups"] == value


def test_limits_count_unicode_characters_and_accept_the_boundary(client):
    groups = [{"name": "🌍" * 64, "patterns": ["🌍" * 256] * 16}]
    groups.extend({"name": str(index), "patterns": ["team/"]} for index in range(31))
    reply = client.post("/api/settings", json={"fleet_groups": groups})
    assert reply.status_code == 200, reply.text
    assert reply.json()["fleet_groups"] == groups


def test_bad_file_is_reported_and_can_be_repaired_without_restart(client, caplog):
    store.write_json(config.config_path(), {"fleet_groups": "broken", "max_live_assistants": 2})
    assert config.load().fleet_groups == []
    assert "field=fleet_groups" in caplog.text
    reply = client.get("/api/settings").json()
    assert reply["fleet_groups"] == [] and "must be a list" in reply["fleet_groups_problem"]
    assert client.get("/api/state").json()["config"]["fleet_groups"] == []
    repaired = client.post("/api/settings", json={"fleet_groups": GROUPS})
    assert repaired.status_code == 200
    assert repaired.json()["fleet_groups_problem"] is None
    assert config.load().max_live_assistants == 2


def test_group_save_is_operator_only_and_does_not_change_project_scope(client, fleet):
    alpha, beta = [projects.create(name) for name in ("Alpha", "Beta")]
    for project, slug in ((alpha, "a"), (beta, "b")):
        with scope.bound(project["id"]):
            runs.track("forge.test", "team/widget", slug)
    homes = [projects.run_home(f"forge.test/team/widget/{slug}") for slug in ("a", "b")]
    projects.reconcile(fleet)
    with scope.bound(alpha["id"]):
        credential = config.active_assistant_credential()
    reply = client.post("/api/settings", json={"fleet_groups": GROUPS},
                        headers={"Authorization": f"Bearer {credential}"})
    assert reply.status_code == 403, reply.text
    assert client.post("/api/settings", json={"fleet_groups": GROUPS}).status_code == 200
    assert client.post("/api/assistant/config", json={"fleet_groups": []}).status_code == 400
    for project, slug in ((alpha, "a"), (beta, "b")):
        state = client.get("/api/state", params={"workspace_id": project["id"]}).json()
        assert [row["slug"] for row in state["runs"]] == [slug]
        assert state["config"]["fleet_groups"] == GROUPS
    assert [projects.run_home(f"forge.test/team/widget/{slug}") for slug in ("a", "b")] == homes
    assert client.post("/api/settings", json={"fleet_groups": []}, headers={"Authorization": "Bearer invalid"}).status_code == 401


def test_fleet_groups_editor_and_map_in_the_dom():
    node = node_binary()
    if not node:
        require_node_toolchain("Node is required for fleet group editor tests")
    result = subprocess.run(
        [node, "node_modules/vitest/vitest.mjs", "run", "tests/PlatformSettings.test.js", "tests/FleetMap.test.js"],
        cwd=Path(__file__).resolve().parents[1] / "web", capture_output=True, text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
