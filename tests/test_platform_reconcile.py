"""The project supervisor's pass, shaped for its 2 s cadence (#561).

On a production host the reconcile loop meant to run every 2 s started passes
27-31 s apart: each pass held ``operation()`` from start to finish, scanned the
whole registry once per project, and wrote every row whether or not anything had
changed. These tests pin the replacement's shape — one scan, writes only for
changes, a hold per step — and the two outcomes it must keep: a crash is still
recorded, and a start that raced the pass is not mistaken for one.
"""

import contextlib
import os

import pytest

from lmer_platform import assistant, load, project_context, projects, registry, store
from lmer_platform import config as cfg


@pytest.fixture
def platform(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    monkeypatch.setattr(load, "DB_WRITE_WAIT", load.WaitStats())
    projects.migrate()

    def no_launch(*_a, **_k):
        raise AssertionError("an idle pass must not launch an assistant")

    monkeypatch.setattr(assistant, "start", no_launch)
    return cfg.load()


def _running_with_live_assistant(workspace_id, number):
    """A project whose assistant is up: this test process stands in for it."""
    session_id = f"20261111-000000-{number}-live"
    registry.register(session_id, kind=assistant.KIND, pid=os.getpid())
    projects.bind_session(session_id, workspace_id)
    with projects.database() as db:
        db.execute("UPDATE projects SET desired='running',status='running',reserved=1,retries=0,"
                   "retry_at=0 WHERE id=?", (workspace_id,))
        db.execute("DELETE FROM starts WHERE project_id=?", (workspace_id,))
    return session_id


@pytest.fixture
def two_live_projects(platform):
    beta = projects.create("Beta")["id"]
    _running_with_live_assistant(projects.GENERAL, 1)
    _running_with_live_assistant(beta, 2)
    projects.reconcile(platform)  # settle whatever setup left behind
    return beta


def test_an_idle_pass_writes_nothing_and_scans_the_registry_once(platform, two_live_projects, monkeypatch):
    scans = []
    real = registry.list_sessions

    def counting(*args, **kwargs):
        scans.append(kwargs.get("live_only", True))
        return real(*args, **kwargs)

    monkeypatch.setattr(registry, "list_sessions", counting)
    monkeypatch.setattr(load, "DB_WRITE_WAIT", load.WaitStats())
    projects.reconcile(platform)
    assert load.DB_WRITE_WAIT.snapshot()["count_60s"] == 0, "an idle pass took the write lock"
    assert scans.count(True) == 1, f"the registry was scanned {scans.count(True)} times for 2 projects"
    for workspace_id in (projects.GENERAL, two_live_projects):
        assert projects.get(workspace_id)["status"] == "running"


def test_the_project_lock_is_taken_per_step_never_across_the_pass(platform, two_live_projects, monkeypatch):
    events = []
    real = projects.operation

    @contextlib.contextmanager
    def recording(*args, **kwargs):
        with real(*args, **kwargs):
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

    monkeypatch.setattr(projects, "operation", recording)
    projects.reconcile(platform)
    # One hold per project step, one for the admission check, none nested.
    assert events == ["enter", "exit"] * 3


def test_a_start_that_raced_the_scan_is_not_recorded_as_a_crash(platform, two_live_projects, monkeypatch):
    # The pass's one scan saw nothing live; the assistant is in fact up.
    monkeypatch.setattr(projects, "_live_projects", lambda snap: set())
    projects.reconcile(platform)
    for workspace_id in (projects.GENERAL, two_live_projects):
        row = projects.get(workspace_id)
        assert row["status"] == "running"
        assert row["error"] is None
    with projects.reader() as db:
        assert db.execute("SELECT count(*) FROM starts").fetchone()[0] == 0


def test_an_assistant_that_died_is_still_recorded(platform, two_live_projects):
    for entry in registry.list_sessions(live_only=False):
        registry.remove(entry["id"], force=True)
    projects.reconcile(platform)
    for workspace_id in (projects.GENERAL, two_live_projects):
        row = projects.get(workspace_id)
        assert row["error"] == "Assistant exited unexpectedly."
        assert row["status"] in ("waiting", "failed")


def test_a_queued_start_is_still_admitted_only_through_admitting(platform, monkeypatch):
    """Plan test 11: with ``operation()`` now taken per admission, the launch
    still runs inside ``admitting()`` for its own project, and a start outside
    it is still refused."""
    admitted = []

    def start(config):
        workspace_id = project_context.current()
        projects.require_admission(workspace_id)  # refuses outside admitting()
        admitted.append(workspace_id)

    monkeypatch.setattr(assistant, "start", start)
    beta = projects.create("Beta")["id"]  # queues a start
    projects.reconcile(platform)
    assert beta in admitted
    assert projects.get(beta)["status"] == "running"
    with pytest.raises(projects.ProjectError, match="durable admission queue"):
        projects.require_admission(beta)
