"""Applicability is checked by actual child admission, including equivalent CLI flags."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from lmer_platform import config, projects, registry, slots, spawn
from tests.test_platform_projects import fleet  # noqa: F401
from lmer_platform import answer, lifecycle, resume, runs, project_context as scope
from tests.test_platform_resume import plant_run, HOST, PROJECT, TASKDEF, TARGET, REPO
from work_repo.run_state import derive_slug


@pytest.fixture
def service_fleet(fleet, request):
    request.getfixturevalue("slot_host")
    projects.migrate()
    return replace(fleet, max_concurrent_sessions=5)


def _assign(projects_allowed):
    config.update_stored({"slots": [{"name": "web", "preset": "webapp_dev", "applicable_projects": projects_allowed}]})


@pytest.mark.parametrize("selection", [
    {"slot": "web"}, {"preset": "webapp_dev"}, {"preset": "webapp_alt"},
    {"extra_args": ("--checkout", "/srv/webapp", "--service", "webapp-web")},
])
def test_direct_and_equivalent_service_paths_refuse_ineligible_project(service_fleet, selection):
    alpha, beta = [projects.create(name) for name in ("Alpha", "Beta")]
    _assign([alpha["id"]])
    with pytest.raises(spawn.SpawnError, match="not applicable"):
        spawn.spawn_session(service_fleet, spawn.SpawnRequest("chat", "work", no_repo=True,
                                                            workspace_id=beta["id"], **selection))
    assert registry.list_sessions() == []


def test_empty_and_legacy_assignments_keep_visible_slots_and_refuse_launches(service_fleet):
    config.update_stored({"slots": [{"name": "web", "preset": "webapp_dev"}]})
    row = slots.slot_rows(config.load())[0]
    assert row.to_dict()["needs_project_assignment"] is True
    assert row.reason == "Needs project assignment"
    with pytest.raises(spawn.SpawnError, match="Needs project assignment"):
        spawn.spawn_session(service_fleet, spawn.SpawnRequest("chat", "work", no_repo=True,
                                                            workspace_id="general", slot="web"))
    _assign([])
    with pytest.raises(spawn.SpawnError, match="No applicable projects"):
        spawn.spawn_session(service_fleet, spawn.SpawnRequest("chat", "work", no_repo=True,
                                                            workspace_id="general", preset="webapp_dev"))


def test_shared_slot_concurrent_equivalent_claims_admit_one_and_preserve_holder(service_fleet):
    alpha, beta = [projects.create(name) for name in ("Alpha", "Beta")]
    _assign([alpha["id"], beta["id"]])
    requests = [spawn.SpawnRequest("chat", str(i), no_repo=True, workspace_id=project["id"], preset="webapp_dev")
                for i, project in enumerate((alpha, beta))]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(spawn.spawn_session, service_fleet, request) for request in requests]
        started, refused = [], []
        for future in futures:
            try:
                started.append(future.result())
            except spawn.SlotOccupied as exc:
                refused.append(exc)
    assert len(started) == len(refused) == 1
    holder = registry.read_session(started[0].session_id)
    assert holder["task"]["slot_service"] == "webapp-web"
    _assign([])
    projects.migrate()
    projects.migrate()
    assert registry.read_session(holder["id"]) == holder
    row = slots.slot_rows(config.load())[0]
    assert row.state == slots.SLOT_OCCUPIED
    assert row.occupant["session_id"] == holder["id"]
    assert row.definition.applicable_projects == ()
    with pytest.raises(spawn.SpawnError, match="No applicable projects"):
        spawn.spawn_session(service_fleet, spawn.SpawnRequest("chat", "third", no_repo=True,
                                                            workspace_id="general", preset="webapp_dev"))


def test_archived_project_cannot_acquire_service(service_fleet):
    alpha = projects.create("Alpha")
    projects.stop(alpha["id"])
    _assign([alpha["id"]])
    projects.archive(alpha["id"], {"runs": []})
    with pytest.raises(spawn.SpawnError, match="archived"):
        spawn.spawn_session(service_fleet, spawn.SpawnRequest("chat", "work", no_repo=True,
                                                            workspace_id=alpha["id"], slot="web"))
    projects.restore(alpha["id"])
    result = spawn.spawn_session(service_fleet, spawn.SpawnRequest("chat", "work", no_repo=True,
                                                                 workspace_id=alpha["id"], preset="webapp_dev"))
    assert registry.read_session(result.session_id)["slot"] == "web"


def test_named_slot_cannot_be_redirected_with_later_service_flag(service_fleet):
    _assign(["general"])
    with pytest.raises(spawn.SpawnError, match="cannot be redirected"):
        spawn.spawn_session(service_fleet, spawn.SpawnRequest("chat", "work", no_repo=True,
            workspace_id="general", slot="web", extra_args=("--service", "other-web")))
    assert registry.list_sessions() == []


@pytest.mark.parametrize("first,second", [("webapp_dev", "stack_dev"), ("stack_dev", "webapp_alt")])
def test_equivalent_service_and_group_overlap_excludes_second_claim(service_fleet, first, second):
    config.update_stored({"slots": [
        {"name": "web", "preset": "webapp_dev", "applicable_projects": ["general"]},
        {"name": "stack", "preset": "stack_dev", "applicable_projects": ["general"]},
    ]})
    spawn.spawn_session(service_fleet, spawn.SpawnRequest("chat", "first", no_repo=True,
                                                         workspace_id="general", preset=first))
    with pytest.raises(spawn.SpawnError, match="held|busy|overlap"):
        spawn.spawn_session(service_fleet, spawn.SpawnRequest("chat", "second", no_repo=True,
                                                             workspace_id="general", preset=second))


@pytest.mark.parametrize("verb", ["resume", "answer"])
def test_moved_run_cannot_reuse_ineligible_slot_and_explicit_choice_launches(service_fleet, verb):
    alpha, beta = [projects.create(name) for name in ("Alpha", "Beta")]
    _assign([alpha["id"]])
    record = plant_run(service_fleet, stop_reason="question" if verb == "answer" else "phase",
                       question="Proceed?" if verb == "answer" else None)
    # The fixture's task/target supplies the canonical slug used by the actual resume.
    slug = derive_slug(TASKDEF, TARGET)
    with scope.bound(alpha["id"]):
        tracked = runs.track(HOST, PROJECT, slug, repo=REPO)
    launched = spawn.spawn_session(service_fleet, spawn.SpawnRequest(TASKDEF, TARGET, repo_url=REPO,
                                                                    workspace_id=alpha["id"], slot="web"))
    lifecycle.exit_session(launched.session_id)
    assert spawn.wait_for_exit_recorded(launched.session_id)
    projects.move(tracked.key, beta["id"], expected_project=alpha["id"])
    request_type = answer.AnswerRequest if verb == "answer" else resume.ResumeRequest
    extra = {"answer": "yes"} if verb == "answer" else {}
    request = request_type(HOST, PROJECT, slug, workspace_id=beta["id"], home_revision=2, confirm_project=True, **extra)
    perform = answer.answer_run if verb == "answer" else resume.resume_run
    with pytest.raises(spawn.SpawnError, match="not applicable"):
        perform(service_fleet, request)
    config.update_stored({"slots": [
        {"name": "web", "preset": "webapp_dev", "applicable_projects": [alpha["id"]]},
        {"name": "other", "preset": "other_dev", "applicable_projects": [beta["id"]]},
    ]})
    resumed = perform(service_fleet, replace(request, slot="other"))
    assert registry.read_session(resumed.session.session_id)["slot"] == "other"
    assert projects.run_home(tracked.key)["project_id"] == beta["id"]
    assert record.is_dir()
