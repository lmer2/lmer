"""Tests for the timesheet outbox and writer (issue #604, task T6).

Git runs for real against a local **bare** repository standing in for the work
repo, so the command lines, the change-set guard and the push path are under
test rather than a mocked subprocess. Every repository a test commits in gets
a local identity; the writer sets its own. The properties that matter: one
file per entry at its D15 path; nothing outside ``timesheet.d/`` is ever
committed; a row leaves the outbox only once its push lands, in order, across
refusals, outages and restarts; and the credential never reaches the clone's
config or a recorded error.
"""

import os
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone

import pytest

from lmer_platform import config as cfg
from lmer_platform import store, timesheet_store, timesheet_writer
from lmer_timesheet import Entry, Trigger, entry_path
from tests.conftest import strip_lmer_env

WORK_URL = "https://git.example.com/agents/work.git"
MIRROR_CRED = "mirror-cred-value"
WRITER_CRED = "writer-cred-value"
LEFT_CRED = "crashed-clone-cred-value"
# Generous: the first drain is a real fetch, rebase and push, and a loaded host
# has taken longer than 15 s over it (#668 baseline).
WAIT_SECONDS = 60


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in ("LMER_WORK_REPO", "LMER_WORK_REPO_TOKEN", "GITLAB_TOKEN_worklog",
                 "GITLAB_TOKEN", "GITLAB_TOKEN_git_example_com", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(name, raising=False)
    yield
    timesheet_writer.register_writer(None)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


def _git(*args, cwd=None):
    result = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                            check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _identity(repo):
    _git("config", "user.name", "Fixture Author", cwd=repo)
    _git("config", "user.email", "fixture@example.com", cwd=repo)


@pytest.fixture
def remote(tmp_path):
    """A bare work repo with one seed commit on ``main``."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "main", cwd=seed)
    _identity(seed)
    (seed / "README.md").write_text("work repo\n", encoding="utf-8")
    _git("add", "README.md", cwd=seed)
    _git("commit", "-q", "-m", "seed", cwd=seed)
    bare = tmp_path / "remote.git"
    _git("init", "-q", "--bare", "-b", "main", str(bare))
    _git("push", "-q", str(bare), "main", cwd=seed)
    return bare


@pytest.fixture
def config(platform_root, remote):
    return cfg.load({"work_repo_url": str(remote)})


def _entry(n, *, person="alex", project="widgets", title=None, month=10):
    start = datetime(2026, month, 1, 9, n, tzinfo=timezone.utc)
    return Entry(
        id=f"entry-{n}", person=person, project=project,
        title=title if title is not None else f"work item {n}",
        start=start, end=start + timedelta(minutes=30),
        trigger=Trigger(start="manual", stop="manual"),
    )


def _upstream_files(remote):
    return set(_git("--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main").splitlines())


def _upstream_blob(remote, path):
    return subprocess.run(["git", "--git-dir", str(remote), "cat-file", "blob", f"main:{path}"],
                          check=True, capture_output=True).stdout


def _upstream_subjects(remote):
    return _git("--git-dir", str(remote), "log", "--reverse", "--format=%s", "main").splitlines()


def _refuse_pushes(remote):
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'protected branch: pushes refused' >&2\nexit 1\n",
                    encoding="utf-8")
    hook.chmod(0o755)
    return hook


def _subject(entry):
    return f"timesheet: {entry.person} {entry.project} {entry.start.strftime('%Y-%m-%dT%H:%M:%SZ')}"


# --- store ------------------------------------------------------------------

def test_store_file_is_owner_only(platform_root):
    timesheet_store.pending()
    mode = (platform_root / timesheet_store.DB_FILE).stat().st_mode
    assert oct(mode)[-3:] == "600"


def test_enqueue_of_a_queued_entry_returns_its_row(platform_root):
    entry = _entry(1)
    first = timesheet_store.enqueue(entry)
    assert timesheet_store.enqueue(entry) == first
    assert [row.seq for row in timesheet_store.pending()] == [first]
    with pytest.raises(ValueError, match="already queued"):
        timesheet_store.enqueue(_entry(1, title="something else"))


def test_enqueue_refuses_an_entry_that_cannot_be_placed(platform_root):
    with pytest.raises(ValueError):
        timesheet_store.enqueue(_entry(1, person="../escape"))
    assert timesheet_store.pending() == []


# --- delivery ---------------------------------------------------------------

def test_drain_writes_one_file_per_entry_at_its_path(config, remote):
    entries = [_entry(1), _entry(2, month=11), _entry(3, person="sam", project="gadgets")]
    for entry in entries:
        timesheet_store.enqueue(entry)

    result = timesheet_writer.TimesheetWriter(config).drain_once()

    assert result == timesheet_writer.DrainResult(delivered=3, remaining=0)
    assert timesheet_store.pending() == []
    assert _upstream_files(remote) == {"README.md", *(str(entry_path(e)) for e in entries)}
    for entry in entries:
        assert _upstream_blob(remote, str(entry_path(entry))) == entry.to_yaml().encode()
    assert _upstream_subjects(remote) == ["seed", *(_subject(e) for e in entries)]
    authors = _git("--git-dir", str(remote), "log", "-1", "--format=%an <%ae>", "main")
    assert authors == f"{cfg.DEFAULT_TIME_WRITER_NAME} <{cfg.DEFAULT_TIME_WRITER_EMAIL}>"


def test_writer_identity_comes_from_config(platform_root, remote):
    config = cfg.load({"work_repo_url": str(remote), "time_writer_name": "Time Bot",
                       "time_writer_email": "time-bot@example.com"})
    timesheet_store.enqueue(_entry(1))
    timesheet_writer.TimesheetWriter(config).drain_once()
    assert _git("--git-dir", str(remote), "log", "-1", "--format=%an <%ae>", "main") \
        == "Time Bot <time-bot@example.com>"


def test_clone_is_owner_only_and_separate_from_the_mirror(config):
    timesheet_store.enqueue(_entry(1))
    timesheet_writer.TimesheetWriter(config).drain_once()
    clone = timesheet_writer.clone_path()
    assert oct(clone.stat().st_mode)[-3:] == "700"
    assert clone != config.mirror_path
    assert not config.mirror_path.exists()


def test_stray_file_outside_timesheet_dir_is_refused_and_cleaned(config, remote):
    writer = timesheet_writer.TimesheetWriter(config)
    timesheet_store.enqueue(_entry(1))
    writer.drain_once()
    before = _upstream_subjects(remote)
    clone = timesheet_writer.clone_path()
    (clone / "stray.txt").write_text("not a timesheet\n", encoding="utf-8")
    (clone / "notes").mkdir()
    (clone / "notes" / "deep.md").write_text("nor this\n", encoding="utf-8")
    entry = _entry(2)
    timesheet_store.enqueue(entry)

    result = writer.drain_once()

    assert result.delivered == 0 and result.remaining == 1
    assert "outside timesheet.d/" in result.error and "stray.txt" in result.error
    [row] = timesheet_store.pending()
    assert row.entry_id == entry.id
    assert row.state == timesheet_store.STATE_FAILED and row.attempts == 1
    assert "notes/deep.md" in row.last_error
    assert not (clone / "stray.txt").exists() and not (clone / "notes").exists()
    assert not (clone / str(entry_path(entry))).exists()
    assert _upstream_subjects(remote) == before
    assert _git("status", "--porcelain", "--untracked-files=all", cwd=clone) == ""

    assert writer.drain_once().delivered == 1
    assert timesheet_store.pending() == []
    assert _upstream_files(remote) == {"README.md", str(entry_path(_entry(1))), str(entry_path(entry))}


def test_a_commit_outside_timesheet_dir_is_never_pushed(config, remote, monkeypatch):
    """The status guard sees the working tree; this one sees what the push sends."""
    writer = timesheet_writer.TimesheetWriter(config)
    real_push = timesheet_writer.TimesheetWriter._push

    def push_with_a_planted_commit(self, clone, branch, url):
        (clone / "planted.txt").write_text("not a timesheet\n", encoding="utf-8")
        _git("add", "planted.txt", cwd=clone)
        _git("commit", "-q", "-m", "planted", cwd=clone)
        return real_push(self, clone, branch, url)

    monkeypatch.setattr(timesheet_writer.TimesheetWriter, "_push", push_with_a_planted_commit)
    before = _upstream_subjects(remote)
    entry = _entry(1)
    timesheet_store.enqueue(entry)

    result = writer.drain_once()

    assert result.delivered == 0 and "planted.txt" in result.error
    assert _upstream_subjects(remote) == before
    [row] = timesheet_store.pending()
    assert row.entry_id == entry.id and row.state == timesheet_store.STATE_FAILED
    monkeypatch.setattr(timesheet_writer.TimesheetWriter, "_push", real_push)
    assert writer.drain_once().delivered == 1
    assert _upstream_files(remote) == {"README.md", str(entry_path(entry))}


def test_change_set_reads_renames_and_unquoted_names(config):
    timesheet_store.enqueue(_entry(1))
    timesheet_writer.TimesheetWriter(config).drain_once()
    clone = timesheet_writer.clone_path()
    _git("mv", "README.md", "timesheet.d/moved.yaml", cwd=clone)
    odd = 'notes/a "quoted" name é.txt'
    (clone / "notes").mkdir()
    (clone / odd).write_text("x\n", encoding="utf-8")

    paths = timesheet_writer._changed_paths(clone)

    assert set(paths) == {"timesheet.d/moved.yaml", "README.md", odd}
    assert [p for p in paths if timesheet_writer._outside_timesheet_dir(p)] != []
    assert not timesheet_writer._outside_timesheet_dir("timesheet.d/moved.yaml")
    assert timesheet_writer._outside_timesheet_dir("README.md")


def test_push_refusal_keeps_rows_in_order_and_a_later_drain_delivers(config, remote):
    hook = _refuse_pushes(remote)
    first, second = _entry(1), _entry(2)
    timesheet_store.enqueue(first)
    timesheet_store.enqueue(second)
    writer = timesheet_writer.TimesheetWriter(config)

    result = writer.drain_once()

    assert result.delivered == 0 and result.remaining == 2
    assert "protected branch" in result.error
    head, later = timesheet_store.pending()
    assert (head.entry_id, head.state, head.attempts) == (first.id, timesheet_store.STATE_FAILED, 1)
    assert "push refused" in head.last_error
    assert (later.entry_id, later.state, later.attempts) == (second.id, timesheet_store.STATE_QUEUED, 0)
    status = timesheet_store.outbox_status()
    assert status["depth"] == 2 and status["failed"] == 1
    assert "protected branch" in status["last_error"]
    assert _upstream_subjects(remote) == ["seed"]

    hook.unlink()
    assert writer.drain_once().delivered == 2
    assert timesheet_store.pending() == []
    assert _upstream_subjects(remote) == ["seed", _subject(first), _subject(second)]
    assert timesheet_store.outbox_status()["depth"] == 0


def test_unreachable_remote_keeps_rows_until_it_returns(config, remote, tmp_path):
    timesheet_store.enqueue(_entry(1))
    writer = timesheet_writer.TimesheetWriter(config)
    writer.drain_once()
    away = tmp_path / "away.git"
    remote.rename(away)
    timesheet_store.enqueue(_entry(2))
    timesheet_store.enqueue(_entry(3))

    result = writer.drain_once()

    assert result.delivered == 0 and "fetch failed" in result.error
    assert [row.entry_id for row in timesheet_store.pending()] == ["entry-2", "entry-3"]

    away.rename(remote)
    assert writer.drain_once().delivered == 2
    assert _upstream_subjects(remote)[1:] == [_subject(_entry(n)) for n in (1, 2, 3)]


def test_restart_delivers_queued_rows_in_order(config, remote):
    hook = _refuse_pushes(remote)
    entries = [_entry(n) for n in (1, 2, 3)]
    for entry in entries:
        timesheet_store.enqueue(entry)
    timesheet_writer.TimesheetWriter(config).drain_once()
    assert len(timesheet_store.pending()) == 3
    hook.unlink()
    shutil.rmtree(timesheet_writer.clone_path())

    result = timesheet_writer.TimesheetWriter(cfg.load({"work_repo_url": str(remote)})).drain_once()

    assert result.delivered == 3
    assert timesheet_store.pending() == []
    assert _upstream_subjects(remote) == ["seed", *(_subject(e) for e in entries)]


def test_upstream_commit_made_meanwhile_is_rebased_over(config, remote, tmp_path, monkeypatch):
    other = tmp_path / "other"
    _git("clone", "-q", str(remote), str(other))
    _identity(other)
    real_git = timesheet_writer._git
    pushed = []

    def racing_git(args, **kwargs):
        if args[0] == "push" and not pushed:
            pushed.append(True)
            (other / "elsewhere.md").write_text("another writer\n", encoding="utf-8")
            _git("add", "elsewhere.md", cwd=other)
            _git("commit", "-q", "-m", "meanwhile", cwd=other)
            _git("push", "-q", "origin", "main", cwd=other)
        return real_git(args, **kwargs)

    monkeypatch.setattr(timesheet_writer, "_git", racing_git)
    entry = _entry(1)
    timesheet_store.enqueue(entry)

    result = timesheet_writer.TimesheetWriter(config).drain_once()

    assert pushed and result.delivered == 1
    assert _upstream_subjects(remote) == ["seed", "meanwhile", _subject(entry)]
    assert _upstream_files(remote) == {"README.md", "elsewhere.md", str(entry_path(entry))}
    assert timesheet_store.pending() == []


def test_replay_of_a_pushed_entry_is_idempotent(config, remote):
    entry = _entry(1)
    timesheet_store.enqueue(entry)
    writer = timesheet_writer.TimesheetWriter(config)
    writer.drain_once()
    subjects = _upstream_subjects(remote)

    timesheet_store.enqueue(entry)
    result = writer.drain_once()

    assert result.delivered == 1
    assert timesheet_store.pending() == []
    assert _upstream_subjects(remote) == subjects


def test_a_hand_edited_pushed_entry_counts_as_delivered_and_keeps_the_edit(config, remote):
    """An entry is fixed by editing its file; a replay must not undo that or block the queue."""
    entry = _entry(1)
    timesheet_store.enqueue(entry)
    writer = timesheet_writer.TimesheetWriter(config)
    writer.drain_once()
    edited = _upstream_blob(remote, str(entry_path(entry)))
    subjects = _upstream_subjects(remote)

    timesheet_store.enqueue(_entry(1, title="rewritten"))
    timesheet_store.enqueue(_entry(2))
    result = writer.drain_once()

    assert result.error is None and result.delivered == 2
    assert timesheet_store.pending() == []
    assert _upstream_blob(remote, str(entry_path(entry))) == edited
    assert _upstream_subjects(remote) == subjects + [_subject(_entry(2))]


def test_a_stuck_head_row_holds_back_every_later_row(config, remote):
    writer = timesheet_writer.TimesheetWriter(config)
    head = timesheet_store.enqueue(_entry(1))
    with timesheet_store.database() as db:
        db.execute("UPDATE outbox SET path=? WHERE seq=?", ("timesheet.d/alex/elsewhere.yaml", head))
    later = _entry(2)
    timesheet_store.enqueue(later)

    result = writer.drain_once()

    assert result.delivered == 0 and result.remaining == 2
    assert str(entry_path(later)) not in _upstream_files(remote)
    assert [row.entry_id for row in timesheet_store.pending()] == ["entry-1", "entry-2"]


def test_an_unpushed_local_file_is_not_taken_for_delivered(config, remote):
    """Identical bytes count only when they are upstream, not left in the clone."""
    timesheet_store.enqueue(_entry(1))
    writer = timesheet_writer.TimesheetWriter(config)
    writer.drain_once()
    entry = _entry(2)
    leftover = timesheet_writer.clone_path() / str(entry_path(entry))
    leftover.parent.mkdir(parents=True, exist_ok=True)
    leftover.write_text(entry.to_yaml(), encoding="utf-8")
    timesheet_store.enqueue(entry)

    assert writer.drain_once().delivered == 1
    assert str(entry_path(entry)) in _upstream_files(remote)


def test_notify_wakes_a_started_writer(config, remote):
    writer = timesheet_writer.TimesheetWriter(config, idle_seconds=3600)
    first_drain = threading.Event()
    delivered = threading.Event()
    real_drain = writer.drain_once

    def observed_drain():
        result = real_drain()
        first_drain.set()
        if result.delivered:
            delivered.set()
        return result

    writer.drain_once = observed_drain
    timesheet_writer.register_writer(writer)
    writer.start()
    try:
        assert first_drain.wait(WAIT_SECONDS)
        entry = _entry(1)
        timesheet_store.enqueue(entry)
        assert delivered.wait(WAIT_SECONDS)
    finally:
        writer.stop()
    assert timesheet_store.pending() == []
    assert str(entry_path(entry)) in _upstream_files(remote)


# --- credentials ------------------------------------------------------------

def _route(monkeypatch, mapping):
    """Make git reach local paths for the given URLs, as ``insteadOf`` rules."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(mapping)))
    for index, (url, path) in enumerate(mapping.items()):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{index}", f"url.{path}.insteadOf")
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{index}", url)


def _credentialed(value):
    return WORK_URL.replace("https://", f"https://oauth2:{value}@")


def _assert_absent_from_clone(value):
    clone = timesheet_writer.clone_path()
    for path in (clone / ".git").rglob("*"):
        if path.is_file():
            assert value.encode() not in path.read_bytes(), path


def test_mirror_credential_is_used_and_never_written_to_the_clone(
        platform_root, remote, tmp_path, monkeypatch):
    monkeypatch.setenv("LMER_WORK_REPO_TOKEN", MIRROR_CRED)
    _route(monkeypatch, {_credentialed(MIRROR_CRED): remote, WORK_URL: tmp_path / "nowhere.git"})
    config = cfg.load({"work_repo_url": WORK_URL})
    timesheet_store.enqueue(_entry(1))

    assert timesheet_writer.TimesheetWriter(config).drain_once().delivered == 1

    clone = timesheet_writer.clone_path()
    assert _git("config", "--local", "remote.origin.url", cwd=clone) == WORK_URL
    assert MIRROR_CRED not in (clone / ".git" / "config").read_text(encoding="utf-8")
    _assert_absent_from_clone(MIRROR_CRED)


def test_a_credential_left_in_the_clone_by_a_crash_is_healed_on_the_next_drain(
        platform_root, remote, tmp_path, monkeypatch, caplog):
    # A daemon killed between `git clone <tokenized>` and `remote set-url`
    # leaves the credential in .git/config; the next drain must drop it.
    monkeypatch.setenv("LMER_WORK_REPO_TOKEN", MIRROR_CRED)
    _route(monkeypatch, {_credentialed(MIRROR_CRED): remote, WORK_URL: tmp_path / "nowhere.git"})
    config = cfg.load({"work_repo_url": WORK_URL})
    timesheet_store.enqueue(_entry(1))
    writer = timesheet_writer.TimesheetWriter(config)
    assert writer.drain_once().delivered == 1
    clone = timesheet_writer.clone_path()
    # Not routed: an insteadOf rule would rewrite `remote get-url` itself.
    _git("config", "remote.origin.url", _credentialed(LEFT_CRED), cwd=clone)
    assert LEFT_CRED in (clone / ".git" / "config").read_text(encoding="utf-8")

    timesheet_store.enqueue(_entry(2))
    with caplog.at_level("DEBUG", logger="lmer_platform.timesheet_writer"):
        assert writer.drain_once().delivered == 1

    assert LEFT_CRED not in (clone / ".git" / "config").read_text(encoding="utf-8")
    assert _git("config", "--local", "remote.origin.url", cwd=clone) == WORK_URL
    _assert_absent_from_clone(LEFT_CRED)
    assert LEFT_CRED not in caplog.text and MIRROR_CRED not in caplog.text
    assert {str(entry_path(_entry(1))), str(entry_path(_entry(2)))} <= _upstream_files(remote)


def test_writer_credential_overrides_the_mirrors(platform_root, remote, tmp_path, monkeypatch):
    monkeypatch.setenv("LMER_WORK_REPO_TOKEN", MIRROR_CRED)
    monkeypatch.setenv(cfg.ENV_TIME_WRITER_CREDENTIAL, WRITER_CRED)
    _route(monkeypatch, {
        _credentialed(WRITER_CRED): remote,
        _credentialed(MIRROR_CRED): tmp_path / "nowhere.git",
        WORK_URL: tmp_path / "nowhere.git",
    })
    config = cfg.load({"work_repo_url": WORK_URL})
    timesheet_store.enqueue(_entry(1))

    assert timesheet_writer.TimesheetWriter(config).drain_once().delivered == 1
    _assert_absent_from_clone(WRITER_CRED)


def test_recorded_errors_are_scrubbed(platform_root, monkeypatch, caplog):
    monkeypatch.setenv("LMER_WORK_REPO_TOKEN", "leaky-cred")
    config = cfg.load({"work_repo_url": WORK_URL})
    timesheet_store.enqueue(_entry(1))

    def failing_run(args, **kwargs):
        stderr = f"fatal: unable to access '{args[-2] if len(args) > 2 else ''}'".encode()
        return subprocess.CompletedProcess(args, 128, b"", stderr)

    monkeypatch.setattr(timesheet_writer.subprocess, "run", failing_run)
    with caplog.at_level("DEBUG", logger="lmer_platform.timesheet_writer"):
        result = timesheet_writer.TimesheetWriter(config).drain_once()

    assert "clone failed" in result.error
    assert "git.example.com" in result.error
    assert "leaky-cred" not in result.error
    assert "leaky-cred" not in timesheet_store.pending()[0].last_error
    assert "leaky-cred" not in caplog.text


# --- written entries --------------------------------------------------------

def test_written_entries_lists_what_landed(config, remote):
    assert timesheet_writer.written_entries("alex") == []
    mine = [_entry(1), _entry(2, month=11)]
    for entry in [*mine, _entry(3, person="sam")]:
        timesheet_store.enqueue(entry)
    writer = timesheet_writer.TimesheetWriter(config)
    writer.drain_once()
    _refuse_pushes(remote)
    timesheet_store.enqueue(_entry(4))
    writer.drain_once()

    written = timesheet_writer.written_entries("alex")

    assert [w.id for w in written] == [e.id for e in mine]
    for item, entry in zip(written, mine):
        assert item.path == str(entry_path(entry))
        assert item.raw == entry.to_yaml().encode()
        assert item.entry.title == entry.title and item.problems == []
    assert [w.id for w in timesheet_writer.written_entries("sam")] == ["entry-3"]
    assert timesheet_writer.written_entries("../alex") == []


def test_no_work_repo_is_recorded_on_the_head_row(platform_root):
    timesheet_store.enqueue(_entry(1))
    result = timesheet_writer.TimesheetWriter(cfg.load()).drain_once()
    assert "no work repo configured" in result.error
    assert "no work repo configured" in timesheet_store.outbox_status()["last_error"]
    assert not os.path.exists(timesheet_writer.clone_path())
