"""Per-project time settings, the stable project key and handler discovery (issue #604).

Projects live in an isolated platform dir (the ``fleet`` fixture); handler
definitions come from a fixture mirror under ``tmp_path``.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from fastapi.testclient import TestClient
import pytest

from lmer_platform import api, assistant, config, projects, timesheet_handlers
from lmer_platform import project_context as scope
from lmer_timesheet import TIMESHEET
from tests.test_platform_projects import fleet  # noqa: F401


@pytest.fixture
def client(fleet):
    projects.migrate()
    secret = config.ensure_secret(fleet)
    app = api.create_app(fleet, secret, state_builder=lambda *_a, **_k: {"runs": [], "sessions": [], "attention": []})
    with TestClient(app, headers={"Authorization": f"Bearer {secret}"}) as http:
        yield http


def _project(http, name):
    created = http.post("/api/projects", json={"name": name})
    assert created.status_code == 201, created.text
    return created.json()


def _patch_settings(http, project, settings):
    return http.patch(f"/api/projects/{project['id']}", json={"settings": settings})


def test_time_settings_round_trip_through_patch(client):
    project = _project(client, "Webshop")
    settings = {"time_project_key": "webshop", "time_handler": "tracker-x",
                "time_clients": [" acme ", "globex"], "time_idle_grace_minutes": "20"}
    updated = _patch_settings(client, project, settings)
    assert updated.status_code == 200, updated.text
    expected = {"time_project_key": "webshop", "time_handler": "tracker-x",
                "time_clients": ["acme", "globex"], "time_idle_grace_minutes": 20}
    assert updated.json()["settings"] == expected
    listed = {row["id"]: row for row in client.get("/api/projects").json()["projects"]}
    assert listed[project["id"]]["settings"] == expected


@pytest.mark.parametrize("settings, message", [
    ({"time_project_key": "Web Shop"}, "time_project_key must be"),
    ({"time_project_key": "-webshop"}, "time_project_key must be"),
    ({"time_project_key": "web--shop"}, "time_project_key must be"),
    ({"time_project_key": "w" * 65}, "time_project_key must be"),
    ({"time_project_key": 7}, "time_project_key must be"),
    ({"time_handler": ""}, "time_handler must be"),
    ({"time_handler": "tracker x"}, "time_handler must be"),
    ({"time_clients": "acme"}, "time_clients must be a list"),
    ({"time_clients": ["acme", "  "]}, "client label"),
    ({"time_clients": ["x" * 33]}, "client label"),
    ({"time_clients": ["acme", "ACME"]}, "same client label twice"),
    ({"time_clients": [f"c{n}" for n in range(51)]}, "at most 50"),
    ({"time_idle_grace_minutes": 0}, "time_idle_grace_minutes is unusable"),
    ({"time_idle_grace_minutes": "soon"}, "time_idle_grace_minutes is unusable"),
    ({"time_idle_grace_minutes": True}, "time_idle_grace_minutes is unusable"),
])
def test_invalid_time_settings_are_refused(client, settings, message):
    project = _project(client, "Webshop")
    refused = _patch_settings(client, project, settings)
    assert refused.status_code == 400, refused.text
    assert message in refused.json()["detail"]["message"]
    assert projects.get(project["id"])["settings"] == {}


def test_default_key_is_a_slug_of_the_name_and_stored_on_first_use(client):
    project = _project(client, "Web Shop: Ünïcode!")
    assert "time_project_key" not in projects.get(project["id"])["settings"]
    assert projects.project_key(project["id"]) == "web-shop-unicode"
    assert projects.get(project["id"])["settings"]["time_project_key"] == "web-shop-unicode"
    assert projects.project_key(projects.GENERAL) == "general"


def test_a_name_with_no_ascii_letters_gets_the_fallback_key(client):
    project = _project(client, "日本語")
    assert projects.project_key(project["id"]) == projects.FALLBACK_PROJECT_KEY


def test_a_project_key_never_holds_the_file_name_separator(client):
    refused = _patch_settings(client, _project(client, "Webshop"), {"time_project_key": "web--shop"})
    assert "'--'" in refused.json()["detail"]["message"]
    assert _patch_settings(client, _project(client, "Tools"), {"time_handler": "a--b"}).status_code == 200
    derived = _project(client, "Web -- Shop")
    assert projects.project_key(derived["id"]) == "web-shop"


def test_rename_keeps_the_stored_key(client):
    project = _project(client, "Webshop")
    assert projects.project_key(project["id"]) == "webshop"
    renamed = client.patch(f"/api/projects/{project['id']}", json={"name": "Storefront"})
    assert renamed.status_code == 200, renamed.text
    assert projects.project_key(project["id"]) == "webshop"


def test_a_settings_write_that_omits_or_nulls_the_key_keeps_it(client):
    project = _project(client, "Webshop")
    projects.project_key(project["id"])
    assert _patch_settings(client, project, {"supervisor_authority": "full"}).status_code == 200
    assert _patch_settings(client, project, {"time_project_key": None}).status_code == 200
    client.patch(f"/api/projects/{project['id']}", json={"name": "Storefront"})
    assert projects.get(project["id"])["settings"]["time_project_key"] == "webshop"
    assert projects.project_key(project["id"]) == "webshop"


def test_two_names_with_the_same_slug_get_distinct_keys(client):
    first, second, third = (_project(client, name) for name in ("Web Shop", "web-shop", "Web  Shop!"))
    keys = [projects.project_key(item["id"]) for item in (first, second, third)]
    assert len(set(keys)) == 3
    assert set(keys) == {"web-shop", "web-shop-2", "web-shop-3"}


def test_concurrent_first_use_stores_one_key_each(client):
    rows = [_project(client, name) for name in ("Ops", "OPS!", "ops.", "Ops?")]
    with ThreadPoolExecutor(max_workers=4) as pool:
        keys = list(pool.map(lambda row: projects.project_key(row["id"]), rows))
    assert len(set(keys)) == 4
    assert keys == [projects.project_key(row["id"]) for row in rows]


def test_duplicate_explicit_key_is_refused(client):
    first, second = _project(client, "Alpha"), _project(client, "Beta")
    assert _patch_settings(client, first, {"time_project_key": "shared"}).status_code == 200
    refused = _patch_settings(client, second, {"time_project_key": "shared"})
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "project_key_taken"
    # Beta has no stored key yet, but its name already claims "beta".
    refused = _patch_settings(client, first, {"time_project_key": "beta"})
    assert refused.status_code == 409, refused.text
    assert projects.project_key(first["id"]) == "shared"
    # Re-sending a project's own key is not a collision.
    assert _patch_settings(client, first, {"time_project_key": "shared"}).status_code == 200


def test_derived_key_steps_around_an_explicit_one(client):
    first, second = _project(client, "Alpha"), _project(client, "Gamma")
    assert _patch_settings(client, first, {"time_project_key": "gamma"}).status_code == 409
    assert _patch_settings(client, first, {"time_project_key": "alpha-team"}).status_code == 200
    renamed = client.patch(f"/api/projects/{second['id']}", json={"name": "Alpha Team"})
    assert renamed.status_code == 200, renamed.text
    assert projects.project_key(second["id"]) == "alpha-team-2"


def test_add_client_label_persists_and_keeps_existing_labels(client):
    project = _project(client, "Webshop")
    assert _patch_settings(client, project, {"time_clients": ["acme"]}).status_code == 200
    projects.add_client_label(project["id"], " globex ")
    projects.add_client_label(project["id"], "ACME")
    assert projects.get(project["id"])["settings"]["time_clients"] == ["acme", "globex"]
    with pytest.raises(projects.ProjectError, match="client label"):
        projects.add_client_label(project["id"], "")


def test_add_client_label_is_atomic_under_concurrent_callers(client):
    project = _project(client, "Webshop")
    labels = [f"client-{n}" for n in range(12)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda label: projects.add_client_label(project["id"], label), labels))
    assert sorted(projects.get(project["id"])["settings"]["time_clients"]) == sorted(labels)


def test_a_patch_carrying_time_clients_replaces_the_list(client):
    project = _project(client, "Webshop")
    projects.add_client_label(project["id"], "acme")
    projects.add_client_label(project["id"], "globex")
    assert _patch_settings(client, project, {"time_clients": ["initech"]}).status_code == 200
    assert projects.get(project["id"])["settings"]["time_clients"] == ["initech"]


def test_time_settings_never_reach_an_assistant_launch(client):
    project = _project(client, "Webshop")
    assert _patch_settings(client, project, {"time_handler": "tracker-x", "model": "fixture-model"}).status_code == 200
    projects.project_key(project["id"])
    with scope.bound(project["id"]):
        values, _sources = assistant._launch_settings(None)
    assert values["model"] == "fixture-model"
    assert not set(values) & set(projects.PROJECT_TIME_SETTING_KEYS)


@pytest.fixture
def mirror(fleet, tmp_path):
    root = tmp_path / "mirror"
    handlers = root / "timesheet.d" / ".handlers"
    handlers.mkdir(parents=True)
    (handlers / "tracker-x.yaml").write_text(
        "name: tracker-x\nlabel: Tracker X\nsync: true\n"
        "fields:\n  - key: task_id\n    label: Tracker X task\n    required: true\n"
    )
    (handlers / "broken.yaml").write_text("name: broken\nfields: nope\n")
    return replace(fleet, work_repo_mirror=str(root))


def test_handler_listing_reads_the_mirror_and_always_has_timesheet(mirror):
    handlers, problems = timesheet_handlers.available(mirror)
    assert set(handlers) == {"timesheet", "tracker-x"}
    assert handlers["timesheet"] is TIMESHEET
    assert handlers["tracker-x"].label == "Tracker X"
    assert handlers["tracker-x"].get_field("task_id").required
    assert any(problem.startswith("timesheet.d/.handlers/broken.yaml:") for problem in problems)


def test_handler_listing_without_a_mirror_is_just_timesheet(fleet, tmp_path):
    handlers, problems = timesheet_handlers.available(replace(fleet, work_repo_mirror=str(tmp_path / "absent")))
    assert handlers == {"timesheet": TIMESHEET}
    assert problems == []


def test_resolve_a_defined_handler(mirror):
    handler, warning = timesheet_handlers.resolve(mirror, "tracker-x")
    assert handler.name == "tracker-x"
    assert warning is None
    assert timesheet_handlers.resolve(mirror, None) == (TIMESHEET, None)
    assert timesheet_handlers.resolve(mirror, "timesheet") == (TIMESHEET, None)


def test_an_undefined_handler_resolves_to_timesheet_with_a_warning(mirror):
    handler, warning = timesheet_handlers.resolve(mirror, "tracker-y")
    assert handler is TIMESHEET
    assert "'tracker-y' is not defined" in warning
    handler, warning = timesheet_handlers.resolve(mirror, "broken")
    assert handler is TIMESHEET
    assert "'broken' is unusable" in warning and "fields" in warning


# --- the issue map (issue #665, D25) ----------------------------------------

ISSUE = "https://git.example.com/acme/widgets/-/issues/12"


def _write_map(config, text):
    path = config.mirror_path / "timesheet.d" / ".handlers" / "tracker-x" / "issues.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_issue_map_reads_the_mirror(mirror):
    handler, _ = timesheet_handlers.lookup(mirror, "tracker-x")
    assert timesheet_handlers.issue_map(mirror, handler) == ({}, [])
    _write_map(mirror, f"{ISSUE}/:\n  task_id: 86c1x2y3z\n")
    assert timesheet_handlers.issue_map(mirror, handler) == ({ISSUE: {"task_id": "86c1x2y3z"}}, [])
    assert timesheet_handlers.issue_map(mirror, TIMESHEET) == ({}, [])


def test_issue_map_is_read_again_when_the_file_changes(mirror):
    handler, _ = timesheet_handlers.lookup(mirror, "tracker-x")
    path = _write_map(mirror, f"{ISSUE}:\n  task_id: aaaaaaaaa\n")
    assert timesheet_handlers.issue_map(mirror, handler)[0][ISSUE] == {"task_id": "aaaaaaaaa"}
    path.write_text(f"{ISSUE}:\n  task_id: bbbbbbbbbbbb\n")
    assert timesheet_handlers.issue_map(mirror, handler)[0][ISSUE] == {"task_id": "bbbbbbbbbbbb"}
    path.unlink()
    assert timesheet_handlers.issue_map(mirror, handler) == ({}, [])


def test_a_malformed_issue_map_is_a_handler_problem_and_the_handler_still_works(mirror):
    _write_map(mirror, "- not\n- a mapping\n")
    handlers, problems = timesheet_handlers.available(mirror)
    assert "tracker-x" in handlers
    assert "timesheet.d/.handlers/tracker-x/issues.yaml: not a mapping, but a list" in problems
    handler, warning = timesheet_handlers.lookup(mirror, "tracker-x")
    assert handler.name == "tracker-x" and warning is None
