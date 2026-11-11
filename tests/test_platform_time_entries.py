"""The entries join against real git on both sides (issue #604, spec D18).

The writer's clone fills in entries the work-repo mirror cannot hold yet, and
only those: once the mirror has pulled past what the clone last saw upstream,
the mirror is the whole truth, so an entry deleted or moved upstream leaves the
page even while the clone's ``origin`` ref is stale. Git runs for real against
a local bare repository; the mirror is the daemon's own shallow mirror.
"""
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from lmer_platform import (
    api, comms, projects, timesheet_store, timesheet_writer, timetrack, timetrack_entries, workrepo,
)
from lmer_platform import config as cfg
from lmer_platform.timetrack import Tracker
from lmer_platform.timetrack_runs import RunFacts
from lmer_timesheet import Entry, SessionRef, Trigger, entry_path, parse_handler
from tests.test_platform_projects import fleet  # noqa: F401
from tests.test_platform_timetrack import Clock

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
PERSON = "alex"
PEOPLE = [{"key": PERSON, "name": "Alex", "slack": ["U0ALEX"]}]


def _git(*args, cwd=None):
    result = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                            check=True, capture_output=True, text=True)
    return result.stdout.strip()


@pytest.fixture
def remote(tmp_path):
    """A bare work repo, and a working clone of it that stands for a person's edits."""
    bare = tmp_path / "remote.git"
    _git("init", "-q", "--bare", "-b", "main", str(bare))
    hand = tmp_path / "hand"
    _git("init", "-q", "-b", "main", str(hand))
    _git("config", "user.name", "Fixture Author", cwd=hand)
    _git("config", "user.email", "fixture@example.com", cwd=hand)
    (hand / "README.md").write_text("work repo\n", encoding="utf-8")
    _git("add", "README.md", cwd=hand)
    _git("commit", "-q", "-m", "seed", cwd=hand)
    _git("push", "-q", str(bare), "main", cwd=hand)
    _git("remote", "add", "origin", str(bare), cwd=hand)
    return bare, hand


@pytest.fixture
def platform(fleet, remote, tmp_path, monkeypatch):
    projects.migrate()
    monkeypatch.setattr(comms, "_HUB", comms.CommsHub())
    return replace(fleet, people=[dict(person) for person in PEOPLE], default_person=PERSON,
                   work_repo_url=str(remote[0]), work_repo_mirror=str(tmp_path / "mirror"))


@pytest.fixture
def client(platform):
    timetrack.register_tracker(Tracker(platform, now=Clock(T0)))
    secret = cfg.ensure_secret(platform)
    app = api.create_app(platform, secret, state_builder=lambda *_a, **_k: {"runs": []})
    try:
        with TestClient(app, headers={"Authorization": f"Bearer {secret}"}) as http:
            yield http
    finally:
        timetrack.register_tracker(None)


def _entry(entry_id, day):
    start = datetime(2026, 9, day, 10, 0, tzinfo=timezone.utc)
    return Entry(id=entry_id, person=PERSON, project="webshop", title=f"work {entry_id}",
                 start=start, end=start + timedelta(hours=1),
                 trigger=Trigger(start="manual", stop="manual"))


def _written(http):
    response = http.get("/api/time/entries")
    assert response.status_code == 200, response.text
    return {row["id"]: row["origin"] for row in response.json()["rows"] if row["stage"] == "written"}


def _pull(platform):
    status = workrepo.pull(platform, force=True)
    assert status.last_pull_ok, status.last_error
    return status


def _clone_origin():
    return _git("rev-parse", "refs/remotes/origin/main", cwd=timesheet_writer.clone_path())


def test_an_entry_deleted_upstream_leaves_the_page_while_the_clone_is_stale(platform, client, remote):
    _bare, hand = remote
    assert workrepo.ensure_clone(platform).present
    wrong, right = _entry("e-wrong", 1), _entry("e-right", 2)
    for entry in (wrong, right):
        timesheet_store.enqueue(entry)
    assert timesheet_writer.TimesheetWriter(platform).drain_once().delivered == 2

    assert _written(client) == {"e-wrong": "clone", "e-right": "clone"}, "pushed, not yet pulled"
    _pull(platform)
    assert _written(client) == {"e-wrong": "mirror", "e-right": "mirror"}

    _git("pull", "-q", "origin", "main", cwd=hand)
    _git("rm", "-q", str(entry_path(wrong)), cwd=hand)
    _git("commit", "-q", "-m", "drop a wrong entry", cwd=hand)
    _git("push", "-q", "origin", "main", cwd=hand)
    stale = _clone_origin()
    _pull(platform)

    assert _clone_origin() == stale, "the writer has not drained since"
    assert _written(client) == {"e-right": "mirror"}


def test_an_entry_pushed_after_the_mirror_pulled_is_still_listed(platform, client, remote):
    assert workrepo.ensure_clone(platform).present
    timesheet_store.enqueue(_entry("e-first", 1))
    writer = timesheet_writer.TimesheetWriter(platform)
    assert writer.drain_once().delivered == 1
    _pull(platform)

    timesheet_store.enqueue(_entry("e-second", 2))
    assert writer.drain_once().delivered == 1

    assert _written(client) == {"e-first": "mirror", "e-second": "clone"}


@pytest.mark.parametrize("name", ["20261345T000000Z--p--x.yaml", "20261027T250000Z--p--x.yaml"])
def test_an_impossible_start_in_a_broken_files_name_is_a_problem_row(name, tmp_path):
    row = timetrack_entries._file_row(f"timesheet.d/alex/2026/10/{name}", b"start: [", "mirror",
                                      {}, timetrack_entries._Ledgers(tmp_path, "alex"), {})
    assert row["start"] is None and row["project"] == "p" and row["problems"]


# --- issues, identity and unassigned per_issue fields (issue #665) ----------

ISSUE_12 = "https://git.example.com/acme/widgets/-/issues/12"
PER_ISSUE = "name: tracker-p\nfields:\n  - key: task_id\n    label: Task\n    required: true\n    per_issue: true\n"


def _handlers():
    handler, problems = parse_handler(PER_ISSUE)
    assert problems == []
    return {"tracker-p": handler}


def _file(tmp_path, entry):
    return timetrack_entries._file_row(str(entry_path(entry)), entry.to_yaml().encode(), "mirror",
                                       _handlers(), timetrack_entries._Ledgers(tmp_path, PERSON), {})


def test_a_written_entry_carries_its_issues(tmp_path):
    entry = Entry(id="e-1", person=PERSON, project="widgets", handler="tracker-p",
                  fields={"task_id": "t12"}, start=T0, end=T0 + timedelta(minutes=5),
                  trigger=Trigger(start="activity", stop="edit"), issues=[ISSUE_12])
    row = _file(tmp_path, entry)
    assert (row["issues"], row["unassigned"], row["problems"]) == ([ISSUE_12], [], [])


def test_an_empty_per_issue_field_is_unassigned_not_a_problem(tmp_path):
    entry = Entry(id="e-2", person=PERSON, project="widgets", handler="tracker-p",
                  start=T0, end=T0 + timedelta(minutes=5), trigger=Trigger(start="activity", stop="idle"))
    row = _file(tmp_path, entry)
    assert (row["unassigned"], row["problems"]) == (["task_id"], [])


def test_an_empty_optional_per_issue_field_is_not_unassigned():
    handler, problems = parse_handler(PER_ISSUE.replace("required: true", "required: false"))
    assert problems == []
    assert timetrack_entries._unassigned(handler, {}) == []


def test_running_and_kept_rows_carry_identity_and_issues(platform, tmp_path):
    clock = Clock(T0)
    tracker = Tracker(platform, now=clock)
    docs = projects.create("Docs")["id"]
    ref = SessionRef(run="git.example.com/acme/widgets/fix-12")
    run = RunFacts(ref=ref, key=ref.run, title="Fix 12", issues=(ISSUE_12,))
    tracker.note_activity(PERSON, docs, at=T0, runs=(run,))
    rows = timetrack_entries._live_rows(tracker, PERSON, {}, {}, skip_ids=set())
    [running] = [row for row in rows if row["stage"] == timetrack_entries.STAGE_RUNNING]
    assert (running["identity"], running["issues"], running["title_source"]) == (
        ref.to_dict(), [ISSUE_12], "run")
    tracker.stop(PERSON, None, at=T0 + timedelta(seconds=5), trigger="manual")
    rows = timetrack_entries._live_rows(tracker, PERSON, {}, {}, skip_ids=set())
    [kept] = [row for row in rows if row["stage"] == timetrack_entries.STAGE_KEPT]
    assert (kept["identity"], kept["issues"]) == (ref.to_dict(), [ISSUE_12])
