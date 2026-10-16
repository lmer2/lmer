"""Tests for the host-side work-repo mirror (issue #141, slices M1 / T4, T45, T55).

The properties that matter: git failures are recorded rather than raised, the
token never reaches disk or an error message, a mirror pointing at a different
repo is refused rather than wiped, the throttle works, one caller at a time
clones the mirror and one fetches it while finding either in flight is not an
error, and run-dir discovery handles project paths of varying depth across the
whole fleet.

Git is exercised for real against local repositories — no network, but also no
mocked subprocess, so the actual command lines are under test. The concurrency
tests keep that going: they race real fetches against a real mirror rather than
asserting that a lock was taken, since a lock that is taken and does not exclude
anything would pass the second kind of test and none of the first.
"""

import contextlib
import errno
import fcntl
import os
import subprocess
import sys
import threading
import time

import pytest
import yaml

from lmer_platform import config as cfg
from lmer_platform import store, workrepo
from tests.conftest import strip_lmer_env


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in ("LMER_WORK_REPO", "LMER_WORK_REPO_TOKEN", "GITLAB_TOKEN_worklog",
                 "GITLAB_TOKEN", cfg.ENV_WORK_REPO_MIRROR):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


def _run(*args, cwd):
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True)


@pytest.fixture
def upstream(tmp_path):
    """A real local git repo shaped like the work repo."""
    repo = tmp_path / "upstream"
    (repo / "gitlab.example.com" / "agents" / "global" / "runs"
     / "develop-1").mkdir(parents=True)
    (repo / "gitlab.example.com" / "agents" / "global" / "runs" / "develop-1" / "state.yaml"
     ).write_text("schema: 1\nstatus: in-progress\n", encoding="utf-8")

    _run("git", "init", "-q", "-b", "main", cwd=repo)
    _run("git", "config", "user.email", "test@example.com", cwd=repo)
    _run("git", "config", "user.name", "Test", cwd=repo)
    _run("git", "add", "-A", cwd=repo)
    _run("git", "commit", "-q", "-m", "seed", cwd=repo)
    return repo


@pytest.fixture
def config(platform_root, upstream):
    return cfg.load({"work_repo_url": str(upstream)})


# --- clone ------------------------------------------------------------------

def test_ensure_clone_creates_mirror(config):
    status = workrepo.ensure_clone(config)
    assert status.present is True
    assert status.healthy is True
    assert status.head_sha
    assert (config.mirror_path / "gitlab.example.com").is_dir()


def test_ensure_clone_is_idempotent(config):
    first = workrepo.ensure_clone(config)
    second = workrepo.ensure_clone(config)
    assert second.present is True
    assert second.head_sha == first.head_sha


def test_clone_does_not_persist_token_in_git_config(platform_root, upstream,
                                                    monkeypatch):
    """A token baked into .git/config would sit on disk for the daemon's life."""
    monkeypatch.setenv("LMER_WORK_REPO_TOKEN", "s3cret-token")
    url = "https://git.example.com/agents/work.git"
    monkeypatch.setattr(
        workrepo, "_authenticated_url", lambda _u: str(upstream)
    )
    config = cfg.load({"work_repo_url": url})

    workrepo.ensure_clone(config)
    git_config = (config.mirror_path / ".git" / "config").read_text(encoding="utf-8")
    assert "s3cret-token" not in git_config
    assert "oauth2" not in git_config


def test_mirror_directory_is_owner_only(config):
    workrepo.ensure_clone(config)
    assert oct(config.mirror_path.stat().st_mode)[-3:] == "700"


def test_missing_work_repo_url_is_recorded_not_raised(platform_root):
    config = cfg.load()
    status = workrepo.ensure_clone(config)
    assert status.present is False
    assert status.healthy is False
    assert "no work repo configured" in status.last_error


def test_clone_failure_is_recorded_not_raised(platform_root, tmp_path):
    config = cfg.load({"work_repo_url": str(tmp_path / "does-not-exist")})
    status = workrepo.ensure_clone(config)
    assert status.present is False
    assert status.last_pull_ok is False
    assert status.last_error


def test_clone_failure_scrubs_credentials_from_error(platform_root, monkeypatch,
                                                     tmp_path):
    """Git echoes the URL it was given; the token must not survive into state."""
    bogus = "https://oauth2:leaky-token@git.example.com/agents/work.git"
    monkeypatch.setattr(workrepo, "_authenticated_url", lambda _u: bogus)
    monkeypatch.setattr(
        workrepo, "_git",
        lambda args, cwd=None: (False, f"fatal: could not read from {bogus}"),
    )
    config = cfg.load({"work_repo_url": "https://git.example.com/agents/work.git"})

    status = workrepo.ensure_clone(config)
    assert "leaky-token" not in (status.last_error or "")
    assert "git.example.com" in status.last_error


def test_partial_clone_directory_is_cleaned_up(platform_root, monkeypatch, tmp_path):
    config = cfg.load({"work_repo_url": str(tmp_path / "nope")})

    def fake_git(args, cwd=None):
        if args[0] == "clone":
            config.mirror_path.mkdir(parents=True, exist_ok=True)
            (config.mirror_path / "half-written").write_text("x", encoding="utf-8")
            return False, "fatal: early EOF"
        return True, ""

    monkeypatch.setattr(workrepo, "_git", fake_git)
    workrepo.ensure_clone(config)
    assert not config.mirror_path.exists()


def test_mirror_from_different_url_is_refused_not_wiped(config, tmp_path, upstream):
    """Wiping an operator's directory is not this code's call to make."""
    workrepo.ensure_clone(config)
    sentinel = config.mirror_path / "gitlab.example.com"
    assert sentinel.is_dir()

    other = cfg.load({"work_repo_url": str(tmp_path / "other-work")})
    status = workrepo.ensure_clone(other)

    assert "remove that directory to re-clone" in status.last_error
    assert sentinel.is_dir(), "the existing mirror must be left intact"


def test_missing_git_binary_is_reported(platform_root, monkeypatch, tmp_path):
    def no_git(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", no_git)
    config = cfg.load({"work_repo_url": str(tmp_path / "x")})
    status = workrepo.ensure_clone(config)
    assert "git is not installed" in status.last_error


def test_git_timeout_is_reported(platform_root, monkeypatch, tmp_path):
    def slow(*_args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=kwargs.get("timeout", 1))

    monkeypatch.setattr(subprocess, "run", slow)
    config = cfg.load({"work_repo_url": str(tmp_path / "x")})
    assert "timed out" in workrepo.ensure_clone(config).last_error


# --- pull -------------------------------------------------------------------

def test_pull_picks_up_new_commits(config, upstream):
    workrepo.ensure_clone(config)
    assert len(workrepo.run_dirs(config)) == 1

    new_run = upstream / "gitlab.example.com" / "agents" / "global" / "runs" / "develop-2"
    new_run.mkdir(parents=True)
    (new_run / "state.yaml").write_text("schema: 1\n", encoding="utf-8")
    _run("git", "add", "-A", cwd=upstream)
    _run("git", "commit", "-q", "-m", "second run", cwd=upstream)

    status = workrepo.pull(config, force=True)
    assert status.healthy is True
    assert [r.slug for r in workrepo.run_dirs(config)] == ["develop-1", "develop-2"]


def test_pull_converges_after_force_push(config, upstream):
    """A rewritten remote history must not leave the mirror stuck."""
    workrepo.ensure_clone(config)

    _run("git", "checkout", "-q", "--orphan", "rewritten", cwd=upstream)
    (upstream / "gitlab.example.com" / "agents" / "global" / "runs" / "develop-1"
     / "state.yaml").write_text("schema: 1\nstatus: complete\n", encoding="utf-8")
    _run("git", "add", "-A", cwd=upstream)
    _run("git", "commit", "-q", "-m", "rewritten history", cwd=upstream)
    _run("git", "branch", "-M", "main", cwd=upstream)

    status = workrepo.pull(config, force=True)
    assert status.healthy is True
    state = (config.mirror_path / "gitlab.example.com" / "agents" / "global" / "runs"
             / "develop-1" / "state.yaml").read_text(encoding="utf-8")
    assert "complete" in state


def test_pull_is_throttled(config, monkeypatch):
    workrepo.ensure_clone(config)
    workrepo.pull(config, force=True)

    calls = []
    real_git = workrepo._git

    def counting_git(args, cwd=None):
        calls.append(args[0])
        return real_git(args, cwd=cwd)

    monkeypatch.setattr(workrepo, "_git", counting_git)
    workrepo.pull(config)
    assert "fetch" not in calls, "a pull inside the interval should be skipped"


def test_pull_runs_again_after_interval_elapses(config, monkeypatch):
    workrepo.ensure_clone(config)
    workrepo.pull(config, force=True)

    later = workrepo._now() + config.work_repo_pull_interval + 1
    monkeypatch.setattr(workrepo, "_now", lambda: later)

    calls = []
    real_git = workrepo._git
    monkeypatch.setattr(
        workrepo, "_git",
        lambda args, cwd=None: (calls.append(args[0]), real_git(args, cwd=cwd))[1],
    )
    workrepo.pull(config)
    assert "fetch" in calls


def test_backwards_clock_pulls_rather_than_stalling(config, monkeypatch):
    workrepo.ensure_clone(config)
    workrepo.pull(config, force=True)

    monkeypatch.setattr(workrepo, "_now", lambda: 0.0)
    assert workrepo._throttle_expired(config) is True


def test_fetch_failure_records_error_and_keeps_mirror(config, monkeypatch):
    workrepo.ensure_clone(config)

    def failing_fetch(args, cwd=None):
        if args[0] == "fetch":
            return False, "fatal: unable to access remote"
        return True, "abc123"

    monkeypatch.setattr(workrepo, "_git", failing_fetch)
    status = workrepo.pull(config, force=True)

    assert status.present is True
    assert status.healthy is False
    assert "fetch failed" in status.last_error
    assert (config.mirror_path / "gitlab.example.com").is_dir()


def test_reset_failure_is_recorded(config, monkeypatch):
    workrepo.ensure_clone(config)

    def failing_reset(args, cwd=None):
        if args[0] == "reset":
            return False, "fatal: could not reset"
        return True, ""

    monkeypatch.setattr(workrepo, "_git", failing_reset)
    status = workrepo.pull(config, force=True)
    assert "reset failed" in status.last_error


def test_successful_pull_clears_previous_error(config, monkeypatch):
    workrepo.ensure_clone(config)

    # Scoped to a nested context on purpose. A bare `monkeypatch.undo()` here
    # would revert EVERY patch this function-scoped monkeypatch holds — including
    # the platform_root fixture's PLATFORM_DIR — and the next pull would then
    # clone into the developer's real ~/.lmer/platform. That actually happened.
    with monkeypatch.context() as patched:
        patched.setattr(
            workrepo, "_git",
            lambda args, cwd=None: (False, "boom") if args[0] == "fetch" else (True, ""),
        )
        assert workrepo.pull(config, force=True).last_error

    status = workrepo.pull(config, force=True)
    assert status.last_error is None
    assert status.healthy is True


# --- one fetch at a time ----------------------------------------------------

def _commit_run(upstream, slug):
    """Add one run dir upstream and commit it, so a fetch has work to do."""
    run = upstream / "gitlab.example.com" / "agents" / "global" / "runs" / slug
    run.mkdir(parents=True)
    (run / "state.yaml").write_text("schema: 1\n", encoding="utf-8")
    _run("git", "add", "-A", cwd=upstream)
    _run("git", "commit", "-q", "-m", f"add {slug}", cwd=upstream)


@contextlib.contextmanager
def _lock_held_elsewhere(config):
    """Hold the mirror's pull lock the way another caller would.

    A separate ``open`` of the same path, because that is the contention
    ``flock`` actually arbitrates: the lock belongs to the open file description,
    so this excludes the code under test even though it runs in this very
    process. That is not a shortcut — it is the daemon's own case, since its
    request handlers share one process (``lmer_platform.api`` says why they are
    threads), and an implementation that cached one fd would exclude nobody
    there.
    """
    fd = os.open(str(workrepo._lock_path(config)), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _lock_is_held(config):
    """Whether the mirror lock is held right now, asked the way a peer would."""
    fd = os.open(str(workrepo._lock_path(config)), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        return False
    finally:
        os.close(fd)  # releases whatever this fd may have just taken


def _record_git_calls(monkeypatch):
    """Collect the git subcommands ``pull`` runs, still running them for real."""
    calls = []
    real_git = workrepo._git
    monkeypatch.setattr(
        workrepo, "_git",
        lambda args, cwd=None: (calls.append(args[0]), real_git(args, cwd=cwd))[1],
    )
    return calls


def _failures_logged(caplog):
    return [r for r in caplog.records if "platform_mirror_failure" in r.message]


def test_racing_pulls_never_fetch_at_the_same_time(config, upstream, monkeypatch,
                                                   caplog):
    """The bug the lock exists for, reproduced against a real mirror.

    Overlapping pulls are ordinary rather than exotic: ``build_state`` pulls while
    serving a request and those handlers run in a threadpool, so two polls of the
    fleet view land in ``pull`` at once. Both then run ``git fetch --depth 1``,
    both rewrite ``.git/shallow``, and git aborts the loser with "shallow file has
    changed since we read it" — which damaged nothing, but was recorded as
    ``last_error`` and flipped a current mirror unhealthy.

    Each thread keeps pulling until one of *its own* fetches has run, so there is
    one recorded window per thread and "they never overlap" is a claim with teeth.
    The fetch is padded because a fetch from a local path is otherwise too fast
    for an overlap to be anything but luck.
    """
    workrepo.ensure_clone(config)
    _commit_run(upstream, "develop-2")

    windows = []
    ledger = threading.Lock()
    real_git = workrepo._git

    def timed_git(args, cwd=None):
        if args[0] != "fetch":
            return real_git(args, cwd=cwd)
        start = time.time()
        time.sleep(0.05)
        result = real_git(args, cwd=cwd)
        with ledger:
            windows.append((threading.get_ident(), start, time.time()))
        return result

    monkeypatch.setattr(workrepo, "_git", timed_git)

    ready = threading.Barrier(4)
    errors = []

    def poll():
        ident = threading.get_ident()
        try:
            ready.wait(timeout=10)
            deadline = time.time() + 30
            while time.time() < deadline:
                workrepo.pull(config, force=True)
                with ledger:
                    if any(w[0] == ident for w in windows):
                        return
                time.sleep(0.01)
            errors.append(f"thread {ident} never got to fetch")
        except Exception as exc:  # pragma: no cover - a failure prints the reason
            errors.append(exc)

    threads = [threading.Thread(target=poll) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, errors
    assert len(windows) == 4, windows
    ordered = sorted(windows, key=lambda w: w[1])
    for earlier, later in zip(ordered, ordered[1:]):
        assert later[1] >= earlier[2], f"two fetches overlapped: {earlier} {later}"

    status = workrepo.mirror_status(config)
    assert status.healthy is True
    assert status.last_error is None
    assert not _failures_logged(caplog), "a serialised fetch has nothing to report"
    assert [r.slug for r in workrepo.run_dirs(config)] == ["develop-1", "develop-2"]


def test_a_pull_that_finds_the_lock_held_reports_success(config, monkeypatch,
                                                         caplog):
    """A fetch already in flight is the state the caller wanted, not a failure.

    Recording an error here would only move the spurious ``last_error`` from
    git's race guard to ours: the mirror is being brought up to date this very
    moment, by someone else.
    """
    workrepo.ensure_clone(config)
    before = workrepo.mirror_status(config)
    calls = _record_git_calls(monkeypatch)

    with _lock_held_elsewhere(config):
        status = workrepo.pull(config, force=True)

    assert "fetch" not in calls, "the holder owns this fetch"
    assert status.last_error is None
    assert status.last_pull_ok is True
    assert status.healthy is True
    assert status.head_sha == before.head_sha
    assert not _failures_logged(caplog)


def test_a_held_lock_does_not_paper_over_a_recorded_failure(config, monkeypatch):
    """Skipping reports the mirror as it is — including someone else's failure."""
    workrepo.ensure_clone(config)

    # Nested context on purpose; test_successful_pull_clears_previous_error says
    # what a bare monkeypatch.undo() would do to PLATFORM_DIR here.
    with monkeypatch.context() as patched:
        patched.setattr(
            workrepo, "_git",
            lambda args, cwd=None: (False, "fatal: no network")
            if args[0] == "fetch" else (True, ""),
        )
        assert "fetch failed" in workrepo.pull(config, force=True).last_error

    with _lock_held_elsewhere(config):
        status = workrepo.pull(config, force=True)

    assert "fetch failed" in (status.last_error or "")
    assert status.healthy is False


def test_the_throttle_is_read_again_once_the_lock_is_held(config, upstream,
                                                          monkeypatch):
    """The caller we queued behind may have made this fetch redundant.

    The pre-lock check is the check-then-act half of the original bug: two
    callers both read an expired throttle before either recorded a pull. Writing
    the timestamp under the lock is only half the fix — the decision has to be
    re-read under it too, or the second caller still fetches.

    The peer is simulated where a peer actually acts: holding the lock, having
    just recorded its own pull. Everything else, throttle included, is real.
    """
    workrepo.ensure_clone(config)  # leaves no throttle stamp: the first check passes
    _commit_run(upstream, "develop-2")

    real_lock = workrepo._mirror_lock

    @contextlib.contextmanager
    def lock_behind_a_peer(cfg):
        with real_lock(cfg) as acquired:
            if acquired:
                workrepo._save_state(last_pull_monotonic=workrepo._now())
            yield acquired

    monkeypatch.setattr(workrepo, "_mirror_lock", lock_behind_a_peer)
    calls = _record_git_calls(monkeypatch)

    status = workrepo.pull(config)
    assert "fetch" not in calls, "the peer's fetch must not be repeated"
    assert status.last_error is None
    assert [r.slug for r in workrepo.run_dirs(config)] == ["develop-1"]

    calls.clear()
    workrepo.pull(config, force=True)
    assert "fetch" in calls, "force still skips the throttle, before and after the lock"
    assert [r.slug for r in workrepo.run_dirs(config)] == ["develop-1", "develop-2"]


def test_the_throttle_stamp_is_written_while_the_lock_is_held(config, monkeypatch):
    """The other half of closing the check-then-act gap.

    Re-reading the throttle under the lock is only sound if the write it re-reads
    also happens there: a timestamp recorded outside the lock can still land
    between another caller's read and its fetch, which is the gap that let two
    callers both decide to fetch.
    """
    workrepo.ensure_clone(config)

    real_save = workrepo._save_state
    held_at_write = []

    def watching_save(**fields):
        if "last_pull_monotonic" in fields:
            held_at_write.append(_lock_is_held(config))
        real_save(**fields)

    monkeypatch.setattr(workrepo, "_save_state", watching_save)
    assert workrepo.pull(config, force=True).healthy is True
    assert held_at_write == [True], "the throttle stamp was written unserialised"


def test_a_killed_holder_does_not_wedge_the_mirror(config, upstream):
    """Why ``flock`` and not a lockfile carrying a PID.

    A PID file needs a reaper, and the reaper needs to be right about a PID the
    kernel may have recycled. An ``flock`` is released when the fd closes or the
    holder dies, so a daemon killed mid-fetch costs the next pull nothing. The
    first half of this test is also the cross-process case: the ``lmer platform
    status`` CLI pulls the same mirror as the running daemon.
    """
    workrepo.ensure_clone(config)
    _commit_run(upstream, "develop-2")

    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, os, sys, time\n"
         "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)\n"
         "fcntl.flock(fd, fcntl.LOCK_EX)\n"
         "sys.stdout.write('held\\n')\n"
         "sys.stdout.flush()\n"
         "time.sleep(60)\n",
         str(workrepo._lock_path(config))],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        assert workrepo.pull(config, force=True).last_error is None
        assert [r.slug for r in workrepo.run_dirs(config)] == ["develop-1"], (
            "another process holds the lock, so this pull must not have fetched"
        )
    finally:
        holder.kill()
        holder.wait(timeout=30)
        holder.stdout.close()

    status = workrepo.pull(config, force=True)
    assert status.healthy is True
    assert [r.slug for r in workrepo.run_dirs(config)] == ["develop-1", "develop-2"]


def test_pull_proceeds_unserialised_when_flock_is_unsupported(config, upstream,
                                                              monkeypatch, caplog):
    """A filesystem without ``flock`` gets the old racy pull, not no pull at all.

    Treating ``ENOLCK`` as "someone else is fetching" would be silent and
    permanent: the mirror would never update again, and would keep calling itself
    healthy.
    """
    workrepo.ensure_clone(config)
    _commit_run(upstream, "develop-2")

    def no_locks(_fd, _op):
        raise OSError(errno.ENOLCK, "no locks available")

    monkeypatch.setattr(workrepo.fcntl, "flock", no_locks)
    status = workrepo.pull(config, force=True)

    assert status.healthy is True
    assert status.last_error is None
    assert [r.slug for r in workrepo.run_dirs(config)] == ["develop-1", "develop-2"]
    assert any("platform_mirror_lock_unsupported" in r.message for r in caplog.records)


def test_pull_proceeds_when_the_lock_file_cannot_be_created(config, upstream,
                                                            monkeypatch, tmp_path,
                                                            caplog):
    """Same trade for an unopenable lock path: update the mirror, say so loudly."""
    workrepo.ensure_clone(config)
    _commit_run(upstream, "develop-2")

    monkeypatch.setattr(
        workrepo, "_lock_path", lambda _cfg: tmp_path / "no-such-dir" / "work.lock"
    )
    status = workrepo.pull(config, force=True)

    assert status.healthy is True
    assert status.last_error is None
    assert [r.slug for r in workrepo.run_dirs(config)] == ["develop-1", "develop-2"]
    assert any("platform_mirror_lock_unusable" in r.message for r in caplog.records)


def test_the_lock_file_is_not_content_in_the_mirror(config):
    """The mirror is a checkout of someone else's repo; the lock is not their file.

    It is also what keeps the platform's "writes no run state" guards honest —
    they compare every file under ``mirror_path`` before and after an operation.
    """
    workrepo.pull(config, force=True)

    lock = workrepo._lock_path(config)
    assert lock.is_file()
    assert config.mirror_path not in lock.parents
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(config.mirror_path),
        capture_output=True, text=True, check=True,
    )
    assert porcelain.stdout == "", "the lock must not show up as untracked content"


# --- one clone at a time -----------------------------------------------------

def test_racing_first_boots_clone_the_mirror_exactly_once(config, upstream,
                                                          monkeypatch, caplog):
    """The same bug as the fetch race, one step earlier, against a real mirror.

    First boot is the one moment every caller can be inside the clone at once:
    the daemon's own startup, the first request handlers it serves (which pull
    from a threadpool), and an ``lmer platform status`` in another shell. Without
    the lock they all handed git the same destination, and whoever arrived after
    the winner created it got "destination path ... already exists and is not an
    empty directory" — recorded as ``clone failed`` on a mirror that was being
    cloned perfectly well.

    Every caller is made to confirm it saw no mirror before starting, so "only
    one clone ran" is a claim about a race and not about four callers politely
    taking turns. The clone is padded because a clone from a local path is
    otherwise over before a second caller can reach it.
    """
    assert not (config.mirror_path / ".git").is_dir()

    windows = []
    ledger = threading.Lock()
    real_git = workrepo._git

    def timed_git(args, cwd=None):
        if args[0] != "clone":
            return real_git(args, cwd=cwd)
        start = time.time()
        time.sleep(0.1)
        result = real_git(args, cwd=cwd)
        with ledger:
            windows.append((threading.get_ident(), start, time.time()))
        return result

    monkeypatch.setattr(workrepo, "_git", timed_git)

    ready = threading.Barrier(4)
    saw_no_mirror = []
    errors = []

    def boot():
        try:
            ready.wait(timeout=10)
            with ledger:
                saw_no_mirror.append(not (config.mirror_path / ".git").is_dir())
            workrepo.ensure_clone(config)
        except Exception as exc:  # pragma: no cover - a failure prints the reason
            errors.append(exc)

    threads = [threading.Thread(target=boot) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, errors
    assert saw_no_mirror == [True] * 4, "these four never raced a first boot"
    assert len(windows) == 1, f"the mirror was cloned {len(windows)} times: {windows}"

    status = workrepo.mirror_status(config)
    assert status.healthy is True
    assert status.last_error is None
    assert not _failures_logged(caplog), "losing a clone race is not a failure"
    assert [r.slug for r in workrepo.run_dirs(config)] == ["develop-1"]


def test_a_first_boot_that_finds_the_lock_held_records_nothing(config, monkeypatch,
                                                               caplog):
    """A peer is cloning right now, which is the state the caller asked for.

    The honest report is the mirror as it stands — absent, because the peer has
    not finished — and *not* an error. Writing ``last_error`` here would only
    move the spurious failure from git's complaint about a full destination to
    our own, and the next poll finds the mirror there.
    """
    config.mirror_path.parent.mkdir(parents=True, exist_ok=True)
    calls = _record_git_calls(monkeypatch)

    with _lock_held_elsewhere(config):
        status = workrepo.ensure_clone(config)
        # pull clones through ensure_clone, so its "never raises, records every
        # failure" contract covers this path too: a peer's clone in flight must
        # not surface as a pull failure either.
        pulled = workrepo.pull(config, force=True)

    assert "clone" not in calls, "the holder owns this clone"
    assert status.present is False, "the peer has not finished; say so plainly"
    assert status.last_error is None
    assert pulled.last_error is None
    assert not _failures_logged(caplog)

    # The lock was the only thing in the way: with it gone, the clone happens.
    assert workrepo.ensure_clone(config).healthy is True


def test_the_mirror_is_looked_for_again_once_the_clone_lock_is_held(config, upstream,
                                                                    monkeypatch):
    """The peer we lost to may have finished while we reached for the lock.

    Taking the lock is only half the fix. A caller that looked for the mirror,
    lost the race, and then acquired the lock the winner had just released would
    still hand git a destination full of somebody else's clone — the same
    check-then-act gap the throttle had. The peer is simulated where a peer
    actually acts: holding the lock, having just cloned.
    """
    real_lock = workrepo._mirror_lock

    @contextlib.contextmanager
    def lock_behind_a_peer(cfg):
        with real_lock(cfg) as acquired:
            if acquired:
                workrepo._clone(cfg, str(upstream))
            yield acquired

    monkeypatch.setattr(workrepo, "_mirror_lock", lock_behind_a_peer)
    calls = _record_git_calls(monkeypatch)

    status = workrepo.ensure_clone(config)

    assert calls.count("clone") == 1, "the peer's clone must not be repeated"
    assert status.present is True
    assert status.last_error is None
    assert [r.slug for r in workrepo.run_dirs(config)] == ["develop-1"]


def test_a_clone_that_really_fails_is_still_recorded(platform_root, tmp_path,
                                                     caplog):
    """The regression this lock could have introduced, pinned.

    ``last_error`` has to keep meaning "the mirror could not be cloned" — a
    remote that is gone, bad credentials, no network. Losing to a peer is the
    only outcome the lock excuses. The retry is the other half: a clone that
    failed while holding the lock must release it, or the first bad boot wedges
    every attempt after it.
    """
    config = cfg.load({"work_repo_url": str(tmp_path / "does-not-exist")})

    status = workrepo.ensure_clone(config)

    assert status.present is False
    assert status.last_pull_ok is False
    assert "clone failed" in status.last_error
    assert _failures_logged(caplog)
    assert not _lock_is_held(config), "the lock outlived the clone that failed"
    assert "clone failed" in (workrepo.ensure_clone(config).last_error or "")


def test_the_clone_lock_works_before_the_platform_directory_exists(config, caplog):
    """First boot is the only time this race is open, so the lock must work then.

    The lock file is a sibling of the mirror, so it needs the mirror's parent
    directory. Creating that directory *after* taking the lock would leave every
    first-boot caller holding nothing — a warning, and the unserialised clone
    this is here to prevent.
    """
    assert not config.mirror_path.parent.exists()

    assert workrepo.ensure_clone(config).healthy is True

    assert workrepo._lock_path(config).is_file()
    assert not [r for r in caplog.records
                if "platform_mirror_lock_unusable" in r.message], (
        "the first boot cloned unserialised: the lock file had nowhere to live"
    )


def test_another_process_cloning_is_waited_out_not_fought(config, upstream):
    """The cross-process first boot, which is not hypothetical.

    ``lmer platform status`` pulls the same mirror as the running daemon, and on
    a fresh host the first of those to arrive is the one that clones it. The
    second must report what it sees rather than clone on top, and — since the
    holder here is killed mid-clone — must not be locked out afterwards either.
    """
    config.mirror_path.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, os, sys, time\n"
         "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)\n"
         "fcntl.flock(fd, fcntl.LOCK_EX)\n"
         "sys.stdout.write('held\\n')\n"
         "sys.stdout.flush()\n"
         "time.sleep(60)\n",
         str(workrepo._lock_path(config))],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        status = workrepo.ensure_clone(config)
        assert status.present is False
        assert status.last_error is None
        assert not config.mirror_path.exists(), "that clone belongs to the holder"
    finally:
        holder.kill()
        holder.wait(timeout=30)
        holder.stdout.close()

    assert workrepo.ensure_clone(config).healthy is True
    assert [r.slug for r in workrepo.run_dirs(config)] == ["develop-1"]


# --- status -----------------------------------------------------------------

def test_status_before_any_clone(platform_root):
    status = workrepo.mirror_status(cfg.load())
    assert status.present is False
    assert status.healthy is False
    assert status.last_pull_at is None


def test_status_to_dict_exposes_staleness_fields(config):
    workrepo.ensure_clone(config)
    payload = workrepo.mirror_status(config).to_dict()
    assert set(payload) >= {
        "present", "url", "last_pull_at", "last_pull_ok", "last_error",
        "head_sha", "healthy",
    }


def test_status_url_is_scrubbed(platform_root, monkeypatch):
    config = cfg.load({
        "work_repo_url": "https://oauth2:tok@git.example.com/agents/work.git"
    })
    assert "tok" not in (workrepo.mirror_status(config).url or "")


def test_unreadable_mirror_state_does_not_break_status(config, caplog):
    workrepo.ensure_clone(config)
    workrepo._state_path().write_text("{not json", encoding="utf-8")

    status = workrepo.mirror_status(config)
    assert status.present is True
    assert any("platform_mirror_state_unreadable" in r.message for r in caplog.records)


# --- run dir discovery ------------------------------------------------------

def test_run_dirs_handles_varying_project_depth(config, upstream):
    """Project paths are not a fixed number of segments."""
    deep = (upstream / "gitlab.example.com" / "group" / "subgroup" / "project"
            / "runs" / "review-mr-1")
    deep.mkdir(parents=True)
    (deep / "state.yaml").write_text("schema: 1\n", encoding="utf-8")
    _run("git", "add", "-A", cwd=upstream)
    _run("git", "commit", "-q", "-m", "deep project", cwd=upstream)

    workrepo.ensure_clone(config)
    refs = {(r.host, r.project, r.slug) for r in workrepo.run_dirs(config)}
    assert ("gitlab.example.com", "agents/global", "develop-1") in refs
    assert ("gitlab.example.com", "group/subgroup/project", "review-mr-1") in refs


def test_run_dirs_requires_a_state_file(config, upstream):
    stray = upstream / "gitlab.example.com" / "agents" / "global" / "runs" / "not-a-run"
    stray.mkdir(parents=True)
    (stray / "README.md").write_text("notes", encoding="utf-8")
    _run("git", "add", "-A", cwd=upstream)
    _run("git", "commit", "-q", "-m", "stray dir", cwd=upstream)

    workrepo.ensure_clone(config)
    assert [r.slug for r in workrepo.run_dirs(config)] == ["develop-1"]


def test_run_dirs_accepts_legacy_state_yml(config, upstream):
    legacy = upstream / "gitlab.example.com" / "agents" / "global" / "runs" / "legacy-run"
    legacy.mkdir(parents=True)
    (legacy / "state.yml").write_text("schema: 1\n", encoding="utf-8")
    _run("git", "add", "-A", cwd=upstream)
    _run("git", "commit", "-q", "-m", "legacy", cwd=upstream)

    workrepo.ensure_clone(config)
    assert "legacy-run" in [r.slug for r in workrepo.run_dirs(config)]


def test_run_dirs_ignores_top_level_runs_dir(config, upstream):
    """A bare `runs/` at the repo root belongs to no project."""
    orphan = upstream / "runs" / "orphan"
    orphan.mkdir(parents=True)
    (orphan / "state.yaml").write_text("schema: 1\n", encoding="utf-8")
    _run("git", "add", "-A", cwd=upstream)
    _run("git", "commit", "-q", "-m", "orphan", cwd=upstream)

    workrepo.ensure_clone(config)
    assert [r.slug for r in workrepo.run_dirs(config)] == ["develop-1"]


def test_run_dirs_empty_without_mirror(platform_root):
    assert workrepo.run_dirs(cfg.load()) == []


def test_resolve_run_dir_finds_a_tracked_run(config):
    workrepo.ensure_clone(config)
    ref = workrepo.resolve_run_dir(config, "gitlab.example.com", "agents/global", "develop-1")
    assert ref is not None
    assert ref.rel_path == "gitlab.example.com/agents/global/runs/develop-1"


def test_resolve_run_dir_missing_is_none(config):
    workrepo.ensure_clone(config)
    assert workrepo.resolve_run_dir(
        config, "gitlab.example.com", "agents/global", "nope"
    ) is None


def test_resolve_run_dir_requires_a_state_file(config, upstream):
    stray = (config.mirror_path / "gitlab.example.com" / "agents" / "global" / "runs"
             / "no-state")
    workrepo.ensure_clone(config)
    stray.mkdir(parents=True, exist_ok=True)
    assert workrepo.resolve_run_dir(
        config, "gitlab.example.com", "agents/global", "no-state"
    ) is None


@pytest.mark.parametrize("args", [
    ("", "agents/global", "develop-1"),
    ("gitlab.example.com", "", "develop-1"),
    ("gitlab.example.com", "agents/global", ""),
])
def test_resolve_run_dir_rejects_empty_parts(config, args):
    assert workrepo.resolve_run_dir(config, *args) is None


def test_resolve_run_dir_without_mirror_is_none(platform_root):
    assert workrepo.resolve_run_dir(
        cfg.load(), "gitlab.example.com", "agents/global", "develop-1"
    ) is None


def _plant_escape_target(config):
    """A run dir, complete with state file, one level *above* the mirror."""
    escaped = config.mirror_path.parent / "elsewhere" / "runs" / "secret"
    escaped.mkdir(parents=True, exist_ok=True)
    (escaped / "state.yaml").write_text(
        "schema: 1\nstatus: in-progress\n", encoding="utf-8"
    )
    return escaped


@pytest.mark.parametrize("args", [
    ("gitlab.example.com", "../../elsewhere", "secret"),
    ("..", "elsewhere", "secret"),
    ("gitlab.example.com", "agents/global", "../../../../../elsewhere/runs/secret"),
])
def test_resolve_run_dir_cannot_walk_out_of_the_mirror(config, args):
    """The identity is index-fed, not request-fed, so nothing upstream of this
    composition has rejected a ``..``: a hand-edited or corrupted index entry
    would otherwise read a run dir belonging to this host rather than the mirror.
    The refusal is the same ``None`` an absent run gets, so the caller learns
    nothing about the path it aimed at."""
    workrepo.ensure_clone(config)
    escaped = _plant_escape_target(config)
    assert (escaped / "state.yaml").is_file(), (
        "the escape target has to exist for this to prove anything"
    )

    assert workrepo.resolve_run_dir(config, *args) is None


def test_resolve_run_dir_refuses_a_run_dir_symlinked_out_of_the_mirror(config):
    """The other half of resolving first: the segments can be innocent and the
    path still leave the mirror, since the work repo can carry symlinks."""
    workrepo.ensure_clone(config)
    escaped = _plant_escape_target(config)
    link = config.mirror_path / "gitlab.example.com" / "agents" / "global" / "runs" / "linked"
    link.symlink_to(escaped, target_is_directory=True)
    assert (link / "state.yaml").is_file(), "the symlink has to reach the target"

    assert workrepo.resolve_run_dir(
        config, "gitlab.example.com", "agents/global", "linked"
    ) is None


def test_run_dir_ref_rel_path(config):
    workrepo.ensure_clone(config)
    ref = workrepo.run_dirs(config)[0]
    assert ref.rel_path == "gitlab.example.com/agents/global/runs/develop-1"
    assert ref.path.is_dir()


# --- named runs: identity vs address (T90) ----------------------------------
#
# The bug these cover, found in the field: a run tracked as ``review-mr-172``
# whose directory in the work repo was ``review-mr-172--review-mr-172`` resolved
# to nothing, so the platform served null phase and status, no run files and a
# fleet row built from session liveness alone. Nothing was doubled — the run was
# *named*, and a named run's directory is ``<slug>--<name>`` (issue #87 D2).

#: The slug every test here uses. One name, because each test gets its own
#: mirror; only ``workrepo._ANNOUNCED`` crosses tests, and the ``announcing``
#: fixture is what isolates that.
SLUG = "named-run"


@pytest.fixture
def announcing(monkeypatch):
    """A fresh set of already-said-once facts, so log assertions mean something.

    ``workrepo._ANNOUNCED`` is process-wide by design (a read path the UI polls
    must not repeat itself), which without this would make the first test to touch
    a run the only one that can see its log line.
    """
    monkeypatch.setattr(workrepo, "_ANNOUNCED", set())


def _plant_run_dir(config, dir_name, *, slug=SLUG, host="gitlab.example.com",
                   project="agents/global", status="in-progress", created=None,
                   reslugged_from=None):
    """A run directory in the mirror named *dir_name* and recording *slug*."""
    path = config.mirror_path / host / project / "runs" / dir_name
    path.mkdir(parents=True, exist_ok=True)
    state = {
        "schema": 1,
        "slug": slug,
        "name": "named",
        "status": status,
    }
    if created is not None:
        state["created"] = created
    if reslugged_from is not None:
        state["reslugged_from"] = reslugged_from
    (path / "state.yaml").write_text(yaml.safe_dump(state), encoding="utf-8")
    return path


def test_resolve_run_dir_finds_the_name_bearing_dir_of_a_named_run(config):
    """The field bug: the run is healthy, only the address the platform derived
    for it stopped existing when the container renamed the dir."""
    workrepo.ensure_clone(config)
    planted = _plant_run_dir(config, f"{SLUG}--the-name")

    ref = workrepo.resolve_run_dir(config, "gitlab.example.com", "agents/global", SLUG)

    assert ref is not None, "a named run must not read as absent from the mirror"
    assert ref.path == planted
    assert ref.dir_name == f"{SLUG}--the-name"
    assert ref.rel_path == f"gitlab.example.com/agents/global/runs/{SLUG}--the-name"


def test_the_run_keeps_its_slug_as_its_identity(config):
    """No re-key: the slug is what the container records, what the next session
    registers under, and what every consumer keyed on it already uses — so the
    correction is to the address alone."""
    workrepo.ensure_clone(config)
    _plant_run_dir(config, f"{SLUG}--the-name")

    ref = workrepo.resolve_run_dir(config, "gitlab.example.com", "agents/global", SLUG)

    assert ref.slug == SLUG


def test_a_settled_run_dir_is_logged_once_naming_both_names(config, caplog,
                                                            announcing):
    workrepo.ensure_clone(config)
    _plant_run_dir(config, f"{SLUG}--the-name", slug=SLUG)

    with caplog.at_level("INFO", logger="lmer_platform.workrepo"):
        workrepo.resolve_run_dir(config, "gitlab.example.com", "agents/global", SLUG)
        said = [r.getMessage() for r in caplog.records
                if "platform_run_dir_settled" in r.getMessage()]
        caplog.clear()
        workrepo.resolve_run_dir(config, "gitlab.example.com", "agents/global", SLUG)
        again = [r.getMessage() for r in caplog.records
                 if "platform_run_dir_settled" in r.getMessage()]

    assert len(said) == 1, "the correction must not be silent"
    assert f"runs/{SLUG} -> runs/{SLUG}--the-name" in said[0]
    assert again == [], "a read path polled every few seconds says this once"


def test_two_candidate_dirs_are_refused_rather_than_guessed(config, caplog,
                                                            announcing):
    """Not a theoretical branch: a taskdef run with no target is slugged after the
    taskdef alone, so the shared work repo really does hold two ``masterplan`` runs
    whose state files both say ``slug: masterplan``. Content cannot separate them
    because both claims are true, and resolving a run's whole record to the wrong
    one of two is worse than reporting it unpushed."""
    workrepo.ensure_clone(config)
    _plant_run_dir(config, f"{SLUG}--one", slug=SLUG)
    _plant_run_dir(config, f"{SLUG}--two", slug=SLUG)

    with caplog.at_level("WARNING", logger="lmer_platform.workrepo"):
        ref = workrepo.resolve_run_dir(
            config, "gitlab.example.com", "agents/global", SLUG
        )

    assert ref is None
    warned = [r.getMessage() for r in caplog.records
              if "platform_run_dir_ambiguous" in r.getMessage()]
    assert len(warned) == 1
    assert f"{SLUG}--one" in warned[0] and f"{SLUG}--two" in warned[0]


def test_a_dir_recording_another_run_is_not_adopted(config):
    """Confirmation is by content, not by prefix: ``develop-1--x`` recording some
    other run's slug is a foreign run that happens to sort next to this one."""
    workrepo.ensure_clone(config)
    _plant_run_dir(config, f"{SLUG}--the-name", slug="someone-else")

    assert workrepo.resolve_run_dir(
        config, "gitlab.example.com", "agents/global", SLUG
    ) is None


def test_a_dir_recording_no_slug_at_all_is_not_adopted(config):
    """A dir that does not *say* it is this run is not confirmed by its name."""
    workrepo.ensure_clone(config)
    path = config.mirror_path / "gitlab.example.com" / "agents" / "global" / "runs"
    (path / f"{SLUG}--the-name").mkdir(parents=True, exist_ok=True)
    (path / f"{SLUG}--the-name" / "state.yaml").write_text(
        "schema: 1\nstatus: in-progress\n", encoding="utf-8"
    )

    assert workrepo.resolve_run_dir(
        config, "gitlab.example.com", "agents/global", SLUG
    ) is None


def test_an_unnamed_run_resolves_exactly_as_before(config):
    """Absent evidence changes nothing: a run still sitting at ``runs/<slug>``
    gets the ref it always got, and carries no address of its own."""
    workrepo.ensure_clone(config)

    ref = workrepo.resolve_run_dir(
        config, "gitlab.example.com", "agents/global", "develop-1"
    )

    assert ref.dir_name is None
    assert ref.rel_path == "gitlab.example.com/agents/global/runs/develop-1"


def test_the_bare_dir_wins_over_a_name_bearing_sibling(config):
    """Both present means the run is where the platform always looked; the
    fallback is for the case where that address does not exist."""
    workrepo.ensure_clone(config)
    _plant_run_dir(config, SLUG)
    _plant_run_dir(config, f"{SLUG}--the-name")

    ref = workrepo.resolve_run_dir(config, "gitlab.example.com", "agents/global", SLUG)

    assert ref.dir_name is None


def test_resolve_run_dir_follows_the_live_run_that_vacated_the_slug(config):
    workrepo.ensure_clone(config)
    successor = f"{SLUG}-v1.2.3"
    planted = _plant_run_dir(
        config, successor, slug=successor, reslugged_from=[SLUG]
    )

    ref = workrepo.resolve_run_dir(
        config, "gitlab.example.com", "agents/global", SLUG
    )

    assert ref is not None
    assert ref.path == planted
    assert ref.slug == SLUG
    assert ref.dir_name == successor


def test_a_terminal_reslugged_run_does_not_keep_the_seed_address(config):
    workrepo.ensure_clone(config)
    successor = f"{SLUG}-v1.2.3"
    _plant_run_dir(
        config,
        successor,
        slug=successor,
        status="complete",
        reslugged_from=[SLUG],
    )

    assert workrepo.resolve_run_dir(
        config, "gitlab.example.com", "agents/global", SLUG
    ) is None


def test_the_newest_live_reslugged_successor_wins_deterministically(config):
    workrepo.ensure_clone(config)
    _plant_run_dir(
        config,
        f"{SLUG}-v1",
        slug=f"{SLUG}-v1",
        created="2026-08-18T10:00:00Z",
        reslugged_from=[SLUG],
    )
    newest = _plant_run_dir(
        config,
        f"{SLUG}-v2",
        slug=f"{SLUG}-v2",
        created="2026-08-19T10:00:00Z",
        reslugged_from=[SLUG],
    )

    ref = workrepo.resolve_run_dir(
        config, "gitlab.example.com", "agents/global", SLUG
    )

    assert ref.path == newest


def test_reslugged_resolution_scans_once_per_mirror_revision(config, monkeypatch):
    workrepo.ensure_clone(config)
    successor = f"{SLUG}-v1.2.3"
    _plant_run_dir(
        config,
        successor,
        slug=successor,
        reslugged_from=[SLUG, "another-vacated-slug"],
    )
    original = workrepo._read_run_state
    reads = []

    def counted(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(workrepo, "_read_run_state", counted)

    first = workrepo.resolve_run_dir(
        config, "gitlab.example.com", "agents/global", SLUG
    )
    first_scan = list(reads)
    second = workrepo.resolve_run_dir(
        config, "gitlab.example.com", "agents/global", "another-vacated-slug"
    )

    assert first.path == second.path
    assert first.path in first_scan
    assert reads == first_scan


def test_reslugged_resolution_rebuilds_when_the_mirror_head_moves(
        config, monkeypatch):
    workrepo.ensure_clone(config)
    successor = f"{SLUG}-v1.2.3"
    _plant_run_dir(
        config, successor, slug=successor, reslugged_from=[SLUG]
    )
    revision = {"sha": "a" * 40}
    monkeypatch.setattr(
        workrepo,
        "mirror_status",
        lambda _config: workrepo.MirrorStatus(
            present=True, last_pull_ok=True, head_sha=revision["sha"]
        ),
    )
    original = workrepo._build_reslugged_index
    builds = []

    def counted(*args):
        builds.append(revision["sha"])
        return original(*args)

    monkeypatch.setattr(workrepo, "_build_reslugged_index", counted)

    assert workrepo.resolve_run_dir(
        config, "gitlab.example.com", "agents/global", SLUG
    ) is not None
    assert workrepo.resolve_run_dir(
        config, "gitlab.example.com", "agents/global", SLUG
    ) is not None
    revision["sha"] = "b" * 40
    assert workrepo.resolve_run_dir(
        config, "gitlab.example.com", "agents/global", SLUG
    ) is not None

    assert builds == ["a" * 40, "b" * 40]


def test_reslugged_resolution_never_descends_into_the_archive(config):
    workrepo.ensure_clone(config)
    archived = (
        config.mirror_path / "gitlab.example.com" / "agents/global" / "runs"
        / "archive"
    )
    archived.mkdir(parents=True)
    (archived / "state.yaml").write_text(
        "schema: 1\nstatus: in-progress\nslug: archived-run\n"
        f"reslugged_from:\n  - {SLUG}\n",
        encoding="utf-8",
    )

    assert workrepo.resolve_run_dir(
        config, "gitlab.example.com", "agents/global", SLUG
    ) is None


# --- the adoption listing keys on the recorded slug too (T90) ----------------
#
# The same bug from the other end. ``iter_run_dirs`` keyed each run by the
# directory it found it in, so a *named* run was offered for adoption under
# ``<slug>--<name>`` — a key nothing else in the platform uses. A run this
# orchestrator already tracks therefore read as untracked in the picker (which
# lists untracked candidates only, so it was offered again), and adopting it filed
# a second index entry for one run.

def test_a_named_run_is_listed_under_the_slug_it_records(config):
    workrepo.ensure_clone(config)
    planted = _plant_run_dir(config, f"{SLUG}--the-name")

    listed = [ref for ref in workrepo.run_dirs(config) if ref.path == planted]

    assert len(listed) == 1, "one directory is one candidate, healed or not"
    assert listed[0].slug == SLUG, "the identity is what the state file records"
    assert listed[0].dir_name == f"{SLUG}--the-name"
    assert listed[0].rel_path == (
        f"gitlab.example.com/agents/global/runs/{SLUG}--the-name"
    ), "the address stays the directory an operator can open"


def test_every_listed_run_resolves_under_the_key_it_is_listed_with(config):
    """The listing is a picker: adopting a candidate tracks it under the key shown
    here, and every verb afterwards resolves that key. A key that resolves to
    nothing would adopt a run straight into a dark row."""
    workrepo.ensure_clone(config)
    _plant_run_dir(config, f"{SLUG}--the-name")
    _plant_run_dir(config, "masterplan--one", slug="masterplan")
    _plant_run_dir(config, "masterplan--two", slug="masterplan")
    _plant_run_dir(config, "odd--dir", slug="some-other-run")

    listed = workrepo.run_dirs(config)

    assert len({ref.slug for ref in listed}) == len(listed), "no run listed twice"
    for ref in listed:
        found = workrepo.resolve_run_dir(config, ref.host, ref.project, ref.slug)
        assert found is not None and found.path == ref.path, (
            f"{ref.slug} is offered but does not resolve back to {ref.rel_path}"
        )


def test_two_dirs_recording_one_slug_are_listed_by_directory_name(config, caplog,
                                                                  announcing):
    """The ``masterplan`` case, which :func:`resolve_run_dir` refuses to guess at
    while naming the way through — track such a run by its directory name. So that
    is what the picker offers, and a shared slug does not collapse two runs into
    one row or take the listing down."""
    workrepo.ensure_clone(config)
    _plant_run_dir(config, f"{SLUG}--one")
    _plant_run_dir(config, f"{SLUG}--two")

    with caplog.at_level("INFO", logger="lmer_platform.workrepo"):
        slugs = [ref.slug for ref in workrepo.run_dirs(config)]

    assert slugs == ["develop-1", f"{SLUG}--one", f"{SLUG}--two"]
    said = [r.getMessage() for r in caplog.records
            if "platform_run_dir_listed_by_name" in r.getMessage()]
    assert len(said) == 2, "each directory says once which name to track it under"


def test_a_named_run_beside_its_bare_dir_is_not_listed_twice(config):
    """Both present is the case where resolving lands on the bare directory, so the
    slug is that one's key and the sibling is offered under its own name."""
    workrepo.ensure_clone(config)
    _plant_run_dir(config, SLUG)
    _plant_run_dir(config, f"{SLUG}--the-name")

    slugs = [ref.slug for ref in workrepo.run_dirs(config)]

    assert slugs == ["develop-1", SLUG, f"{SLUG}--the-name"]


def test_a_dir_recording_a_slug_it_is_not_named_for_is_listed_as_found(config):
    """Honest rather than clever. The container's rename cannot produce this
    directory from that slug, so :func:`resolve_run_dir` would not find it under
    the recorded slug either — naming the directory an operator can see beats
    offering a key that resolves to nothing."""
    workrepo.ensure_clone(config)
    _plant_run_dir(config, "wrong--name", slug="someone-else")

    listed = {ref.slug: ref for ref in workrepo.run_dirs(config)}

    assert "someone-else" not in listed
    assert listed["wrong--name"].dir_name is None


def test_a_dir_recording_no_slug_is_listed_by_its_name(config):
    """A directory that does not *say* which run it is is not healed by its name."""
    workrepo.ensure_clone(config)
    path = _plant_run_dir(config, f"{SLUG}--the-name")
    (path / "state.yaml").write_text("schema: 1\nstatus: in-progress\n",
                                     encoding="utf-8")

    assert [ref.slug for ref in workrepo.run_dirs(config)] == [
        "develop-1", f"{SLUG}--the-name"
    ]


def test_only_a_name_bearing_dir_costs_a_state_file_read(config, monkeypatch):
    """This walks the whole shared work repo — 180-odd run dirs on the one it was
    written against — and parsing a state file per directory is the expensive part.
    Skipping it where the rename grammar cannot fire costs nothing: with no ``--``
    in the name the claim is the directory name whatever the file records.
    """
    workrepo.ensure_clone(config)
    _plant_run_dir(config, "plain-dir", slug="not-what-this-is-listed-as")
    _plant_run_dir(config, f"{SLUG}--the-name")
    read = []
    recorded_slug = workrepo._recorded_slug

    def counting_recorded_slug(path):
        read.append(path.name)
        return recorded_slug(path)

    monkeypatch.setattr(workrepo, "_recorded_slug", counting_recorded_slug)

    slugs = [ref.slug for ref in workrepo.run_dirs(config)]

    assert read == [f"{SLUG}--the-name"]
    assert "plain-dir" in slugs, "and the skipped read would have said the same"


@pytest.mark.parametrize("slug", ["../runs/escape", "escape*", "**", "["])
def test_a_slug_that_is_not_a_directory_name_is_never_globbed(config, slug):
    """The slug becomes half of a glob pattern here, not a path segment.

    ``**`` is the sharp one: pathlib refuses that pattern outright, and a
    ``ValueError`` out of this read path would take the whole fleet view down for
    one hand-edited index entry. The rest would turn a directory lookup into a
    search of the mirror.
    """
    workrepo.ensure_clone(config)
    _plant_run_dir(config, "escape--the-name", slug="escape")

    assert workrepo.resolve_run_dir(
        config, "gitlab.example.com", "agents/global", slug
    ) is None


# --- the schema-form session record (issue #353, D7/D9/D22) -----------------
#
# In schema form the unit of record is the SESSION: one launch owns
# ``<project>/_runs/<slug>/<session-id>[--<name>]/`` and ``_runs/<slug>/`` is a
# grouping level nothing is ever written at (docs/SCHEMA.md, "Sessions and
# runs"). ``<project>`` is the registered SHORT NAME — a flat top-level dir with
# no host segment (D7) — or, for a repo nobody has named yet, the
# ``{host}/{project}/`` host bucket at whatever depth its project-shaped dir
# sits (D9). The legacy shared ``{host}/{project}/runs/<slug>/`` tree is read in
# place and unchanged.

#: Session ids in the schema-form grammar: timestamp-first, so they sort in
#: start order (docs/SCHEMA.md). The second is the later launch.
SESSION_ID = "20260824-165230-2-31245"
LATER_SESSION_ID = "20260825-090000-2-31300"

#: The remote every registered fixture project is bound to, and the identity
#: derived from it. The port is deliberate: it belongs to the registry KEY and
#: never to a host directory (!254 contract decision 2).
FIXTURE_REPO = "ssh://git@gitlab.example.com:2222/acme/widgets.git"
FIXTURE_IDENTITY = ("gitlab.example.com", "acme/widgets")


def _plant_manifest(config, projects, *, name="example-plexus", schema=1):
    """A ``_lmer.yaml`` at the mirror root — the registry IS the identity (D8)."""
    (config.mirror_path / "_lmer.yaml").write_text(
        yaml.safe_dump({"schema": schema, "name": name, "projects": projects}),
        encoding="utf-8",
    )
    return config.mirror_path / "_lmer.yaml"


def _plant_session_run(config, project_dir, slug, session_id, *, state=True,
                       recorded_slug=None, extra=None, area="_runs"):
    """One schema-form session dir, ``<project_dir>/<area>/<slug>/<session-id>/``."""
    path = config.mirror_path.joinpath(
        *project_dir.split("/"), *area.split("/"), slug, session_id
    )
    path.mkdir(parents=True, exist_ok=True)
    if state:
        record = {
            "schema": 1,
            "slug": slug if recorded_slug is None else recorded_slug,
            "status": "in-progress",
        }
        record.update(extra or {})
        (path / "state.yaml").write_text(yaml.safe_dump(record), encoding="utf-8")
    return path


def _by_slug(config):
    return {ref.slug: ref for ref in workrepo.run_dirs(config)}


def test_run_dirs_find_a_flat_short_name_session_dir(config):
    """D7's primary shape: a project dir with NO host segment at all."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    planted = _plant_session_run(config, "widgets", "develop-2", SESSION_ID)

    ref = _by_slug(config)["develop-2"]

    assert ref.path == planted
    assert ref.slug == "develop-2", "the session id is an address, never the key"
    assert (ref.host, ref.project) == FIXTURE_IDENTITY


def test_run_dirs_take_a_flat_project_identity_from_the_registry(config):
    """The path is never the identity (D7/D8): ``widgets/`` says nothing about
    which repo it holds, so the run key comes from the entry's ``repo:`` — and
    the port that makes a distinct registry key never reaches a host name."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    _plant_session_run(config, "widgets", "develop-2", SESSION_ID)

    ref = _by_slug(config)["develop-2"]

    assert ref.host == "gitlab.example.com", "the host directory carries no port"
    assert ref.project == "acme/widgets"
    assert "widgets/" not in f"{ref.host}/{ref.project}", (
        "the short name is the address, not the identity"
    )


def test_schema_2_inventory_preserves_full_endpoint_and_path_case(config):
    workrepo.ensure_clone(config)
    _plant_manifest(config, {
        "upper": {"repo": "ssh://git@gitlab.example.com:2222/Acme/Widgets.git"},
        "lower": {"repo": "ssh://git@gitlab.example.com:2200/acme/widgets.git"},
    }, schema=2)
    _plant_session_run(config, "upper", "develop-upper", SESSION_ID)
    _plant_session_run(config, "lower", "develop-lower", SESSION_ID)

    refs = {ref.slug: (ref.host, ref.project) for ref in workrepo.run_dirs(config)}

    assert refs["develop-upper"] == ("gitlab.example.com:2222", "Acme/Widgets")
    assert refs["develop-lower"] == ("gitlab.example.com:2200", "acme/widgets")


def test_schema_2_source_selection_distinguishes_port_and_path_case(config):
    workrepo.ensure_clone(config)
    _plant_manifest(config, {
        "upper": {"repo": "ssh://git@gitlab.example.com:2222/Acme/Widgets.git"},
        "lower": {"repo": "ssh://git@gitlab.example.com:2200/acme/widgets.git"},
    }, schema=2)
    upper = _plant_session_run(config, "upper", "develop-2", SESSION_ID)
    lower = _plant_session_run(config, "lower", "develop-2", SESSION_ID)

    upper_ref = workrepo.resolve_run_dir(
        config, "gitlab.example.com:2222", "Acme/Widgets", "develop-2"
    )
    lower_ref = workrepo.resolve_run_dir(
        config, "gitlab.example.com:2200", "acme/widgets", "develop-2"
    )

    assert upper_ref is not None and upper_ref.path == upper
    assert lower_ref is not None and lower_ref.path == lower


def test_schema_2_unregistered_remote_identity_keeps_port_and_case(config):
    workrepo.ensure_clone(config)
    _plant_manifest(config, {}, schema=2)

    assert workrepo.primary_remote_identity(
        config, "ssh://git@git.example.com:2222/Acme/Widgets.git"
    ) == ("git.example.com:2222", "Acme/Widgets")


def test_schema_2_inventory_decodes_unregistered_port_bucket(config):
    workrepo.ensure_clone(config)
    _plant_manifest(config, {}, schema=2)
    planted = _plant_session_run(
        config,
        "git.example.com%3A2222/Acme/Widgets",
        "develop-ported",
        SESSION_ID,
    )

    ref = _by_slug(config)["develop-ported"]

    assert ref.path == planted
    assert (ref.host, ref.project) == (
        "git.example.com:2222", "Acme/Widgets",
    )


def test_run_dirs_fall_back_to_the_identity_the_run_recorded(config):
    """A flat dir with no registry entry is not automatically dark: the record
    itself may say which repo it belongs to. The path still says nothing."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"other": {"repo": "git@gitlab.example.com:acme/other.git"}})
    _plant_session_run(config, "widgets", "develop-2", SESSION_ID,
                       extra={"repo": FIXTURE_REPO})

    ref = _by_slug(config)["develop-2"]

    assert (ref.host, ref.project) == FIXTURE_IDENTITY


def test_run_dirs_skip_a_flat_project_with_no_recoverable_identity(config, caplog,
                                                                   announcing):
    """Nothing is derived from the directory name (D9). A candidate offered under
    a key nothing can resolve is worse than one the operator is told about."""
    workrepo.ensure_clone(config)
    _plant_session_run(config, "widgets", "develop-2", SESSION_ID)

    with caplog.at_level("INFO", logger="lmer_platform.workrepo"):
        slugs = [ref.slug for ref in workrepo.run_dirs(config)]

    assert slugs == ["develop-1"], "only the legacy fixture run"
    said = [r.getMessage() for r in caplog.records
            if "platform_run_dir_unidentified" in r.getMessage()]
    assert len(said) == 1 and "widgets" in said[0]


def test_run_dirs_survive_an_unusable_manifest(config):
    """A broken ``_lmer.yaml`` is the validator's problem. This is a read path
    the fleet view polls: it loses the runs whose identity that file held, and
    nothing else."""
    workrepo.ensure_clone(config)
    (config.mirror_path / "_lmer.yaml").write_text("projects: [", encoding="utf-8")
    _plant_session_run(config, "widgets", "develop-2", SESSION_ID)

    assert [ref.slug for ref in workrepo.run_dirs(config)] == ["develop-1"], (
        "the legacy tree still reads"
    )


def test_run_dirs_find_a_host_bucket_session_dir_at_any_depth(config):
    """D9: an unnamed repo's work stays at ``{host}/{project}/``, and the bucket
    descends to the first project-shaped dir at WHATEVER depth — no cap."""
    workrepo.ensure_clone(config)
    _plant_session_run(
        config, "git.example.com/acme/team/sub/widgets", "review-mr-3", SESSION_ID
    )

    ref = _by_slug(config)["review-mr-3"]

    assert (ref.host, ref.project) == ("git.example.com", "acme/team/sub/widgets")
    assert ref.rel_path == (
        f"git.example.com/acme/team/sub/widgets/_runs/review-mr-3/{SESSION_ID}"
    )


def test_run_dirs_read_all_three_shapes_from_one_mirror(config):
    """Co-existence is the point (D16/D20): one mirror holds a migrated project,
    an unnamed repo's bucket and the legacy shared tree at the same time."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    _plant_session_run(config, "widgets", "develop-2", SESSION_ID)
    _plant_session_run(config, "git.example.com/acme/unnamed", "review-mr-3", SESSION_ID)

    found = {ref.slug: (ref.host, ref.project) for ref in workrepo.run_dirs(config)}

    assert found == {
        "develop-2": FIXTURE_IDENTITY,
        "review-mr-3": ("git.example.com", "acme/unnamed"),
        "develop-1": ("gitlab.example.com", "agents/global"),
    }


def test_run_dirs_rel_path_is_the_session_dir_that_holds_the_record(config):
    """The address is the directory an operator can open, in either shape."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    _plant_session_run(config, "widgets", "develop-2", SESSION_ID)

    by_slug = _by_slug(config)

    assert by_slug["develop-2"].rel_path == f"widgets/_runs/develop-2/{SESSION_ID}"
    assert by_slug["develop-2"].rel_path.split("/") == list(
        by_slug["develop-2"].path.relative_to(config.mirror_path).parts
    ), "the address is the path that was found, not a composed one"


def test_run_dirs_never_read_the_grouping_level_as_a_run(config):
    """``_runs/<slug>/`` groups launches and holds nothing (D22). A state file
    left there by hand is not a run — a dir under ``_runs/`` is one only through
    its session children."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    group = config.mirror_path / "widgets" / "_runs" / "develop-2"
    group.mkdir(parents=True)
    (group / "state.yaml").write_text("schema: 1\n", encoding="utf-8")

    assert [ref.slug for ref in workrepo.run_dirs(config)] == ["develop-1"]


def test_run_dirs_require_a_state_file_in_the_session_dir(config):
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    _plant_session_run(config, "widgets", "develop-2", SESSION_ID, state=False)

    assert [ref.slug for ref in workrepo.run_dirs(config)] == ["develop-1"]


def test_run_dirs_skip_the_seed_dir_of_a_session_being_written(config):
    """``_runs/.new-<session-id>-*/`` is transient tooling state mid-rename."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    seed = config.mirror_path / "widgets" / "_runs" / f".new-{SESSION_ID}-a1b2"
    seed.mkdir(parents=True)
    (seed / "state.yaml").write_text("schema: 1\n", encoding="utf-8")

    assert [ref.slug for ref in workrepo.run_dirs(config)] == ["develop-1"]


def test_run_dirs_do_not_descend_into_a_session_dir(config):
    """A session dir is a complete record, not a tree to hunt through — and a
    masterplan bundle inside one is a foreign writer's (D12)."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    session = _plant_session_run(config, "widgets", "develop-2", SESSION_ID)
    nested = session / "masterplan" / "_runs" / "inner" / LATER_SESSION_ID
    nested.mkdir(parents=True)
    (nested / "state.yaml").write_text("schema: 1\nslug: inner\n", encoding="utf-8")

    slugs = sorted(ref.slug for ref in workrepo.run_dirs(config))

    assert slugs == ["develop-1", "develop-2"], "the bundle is opaque"


def test_run_dirs_ignore_archived_sessions(config):
    """One archive home (D13): ``<project>/_archive/_runs/<slug>/<session-dir>/``
    is a retired record, exactly as legacy ``runs/archive/`` never listed."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    _plant_session_run(config, "widgets", "old-run", SESSION_ID, area="_archive/_runs")

    assert [ref.slug for ref in workrepo.run_dirs(config)] == ["develop-1"]


def test_run_dirs_ignore_a_top_level_schema_runs_dir(config):
    """A bare ``_runs/`` at the root of the plexus belongs to no project."""
    workrepo.ensure_clone(config)
    _plant_session_run(config, ".", "orphan", SESSION_ID)

    assert [ref.slug for ref in workrepo.run_dirs(config)] == ["develop-1"]


def test_run_dirs_offer_one_candidate_per_slug_at_its_newest_session(config):
    """Two launches on one target are two records and ONE run identity (D22).

    The listing is the adoption picker and adoption keys on
    ``host/project/slug``, so offering a slug once per session dir would offer
    one key several times — the defect the legacy listing already refuses. The
    address is the newest session dir, which session ids sort into last.
    """
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    _plant_session_run(config, "widgets", "develop-2", SESSION_ID)
    _plant_session_run(config, "widgets", "develop-2", LATER_SESSION_ID)

    listed = [ref for ref in workrepo.run_dirs(config) if ref.slug == "develop-2"]

    assert len(listed) == 1, "one run identity is one candidate"
    assert listed[0].rel_path == f"widgets/_runs/develop-2/{LATER_SESSION_ID}"


def test_run_dirs_keep_the_slug_when_a_session_dir_is_named(config):
    """``work name`` renames the SESSION dir now, not ``runs/<slug>``. The
    identity is untouched by it; only the address moves."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    _plant_session_run(config, "widgets", "develop-2", f"{SESSION_ID}--the-name")

    ref = _by_slug(config)["develop-2"]

    assert ref.dir_name == f"{SESSION_ID}--the-name"
    assert ref.rel_path == f"widgets/_runs/develop-2/{SESSION_ID}--the-name"


def test_run_dirs_key_a_schema_form_run_by_the_slug_it_records(config):
    """Identity is the recorded ``state.slug`` (issue #87 D1), with the grouping
    dir as the fallback for a record that does not say."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    _plant_session_run(config, "widgets", "release-widgets", SESSION_ID,
                       recorded_slug="release-widgets-v0.8.0")

    slugs = sorted(ref.slug for ref in workrepo.run_dirs(config))

    assert slugs == ["develop-1", "release-widgets-v0.8.0"]


def test_run_dirs_fall_back_to_the_grouping_name_when_the_record_is_silent(config):
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    path = _plant_session_run(config, "widgets", "develop-2", SESSION_ID)
    (path / "state.yaml").write_text("schema: 1\n", encoding="utf-8")

    assert sorted(ref.slug for ref in workrepo.run_dirs(config)) == [
        "develop-1", "develop-2"
    ]


def test_run_dirs_list_two_groups_claiming_one_slug_by_their_own_names(config,
                                                                      caplog,
                                                                      announcing):
    """The ``masterplan`` case in schema form: a claim that names more than one
    grouping dir is reported as found, never guessed at."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    _plant_session_run(config, "widgets", "one", SESSION_ID,
                       recorded_slug="masterplan")
    _plant_session_run(config, "widgets", "two", SESSION_ID,
                       recorded_slug="masterplan")

    with caplog.at_level("INFO", logger="lmer_platform.workrepo"):
        slugs = sorted(ref.slug for ref in workrepo.run_dirs(config))

    assert slugs == ["develop-1", "one", "two"]
    said = [r.getMessage() for r in caplog.records
            if "platform_run_dir_listed_by_name" in r.getMessage()]
    assert len(said) == 2


def test_run_dirs_skip_transient_and_archived_dirs_in_the_legacy_tree(config):
    """The same two skips the schema-form half makes, in the shape that has had
    them longest: a ``runs/.new-<session-id>-*`` seed is a directory mid-rename
    (``work_repo.run_state.seed_run_dir``) and ``runs/archive/`` is where a
    finished record is retired to. Neither is a candidate to adopt."""
    workrepo.ensure_clone(config)
    runs_dir = config.mirror_path / "gitlab.example.com" / "agents" / "global" / "runs"
    for name in (f".new-{SESSION_ID}-a1b2", "archive"):
        planted = runs_dir / name
        planted.mkdir(parents=True, exist_ok=True)
        (planted / "state.yaml").write_text("schema: 1\n", encoding="utf-8")

    assert [ref.slug for ref in workrepo.run_dirs(config)] == ["develop-1"]


# --- resolving a tracked run in schema form (issue #353, D7/D9/D22) ----------
#
# The other half of the shape above. ``resolve_run_dir`` is fed an IDENTITY the
# local run index holds — ``(host, project, slug)`` — and has to find that run's
# record wherever this tree keeps it: the legacy ``{host}/{project}/runs/<slug>/``
# directory, the host bucket's ``{host}/{project}/_runs/<slug>/<session-id>/``
# (whose path is still derived from the remote, D9), or a registered project's
# flat ``<short-name>/_runs/<slug>/<session-id>/``, where the short name is only
# discoverable by reading the registry backwards (D7/D8). Nothing is re-keyed:
# the slug stays the identity in every shape and only the address moves.


def _resolve(config, slug, identity=FIXTURE_IDENTITY):
    return workrepo.resolve_run_dir(config, identity[0], identity[1], slug)


def _mirror_contents(config):
    """Every path under the mirror with its size and mtime, for a no-write check."""
    return sorted(
        (str(path.relative_to(config.mirror_path)), path.stat().st_size,
         path.stat().st_mtime_ns)
        for path in config.mirror_path.rglob("*")
        if ".git/" not in f"{path.relative_to(config.mirror_path)}/"
    )


def test_resolve_run_dir_finds_a_flat_short_name_session_dir(config):
    """D7's primary shape: the record is a session dir under a project dir whose
    name is an address the identity cannot be composed from."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    planted = _plant_session_run(config, "widgets", "develop-2", SESSION_ID)

    ref = _resolve(config, "develop-2")

    assert ref is not None, "a migrated project's run must not read as absent"
    assert ref.path == planted
    assert ref.slug == "develop-2", "no re-key: the slug is still the identity"
    assert ref.dir_name == SESSION_ID
    assert ref.rel_path == f"widgets/_runs/develop-2/{SESSION_ID}"


def test_resolve_run_dir_finds_a_host_bucket_session_dir(config):
    """D9: an unnamed repo's work keeps the path derived from its remote, so this
    identity resolves with no registry at all — but the record is still a session
    dir (D22)."""
    workrepo.ensure_clone(config)
    planted = _plant_session_run(
        config, "git.example.com/acme/unnamed", "review-mr-3", SESSION_ID
    )

    ref = workrepo.resolve_run_dir(
        config, "git.example.com", "acme/unnamed", "review-mr-3"
    )

    assert ref is not None
    assert ref.path == planted
    assert ref.rel_path == (
        f"git.example.com/acme/unnamed/_runs/review-mr-3/{SESSION_ID}"
    )


def test_resolve_run_dir_finds_the_name_bearing_session_dir(config):
    """``work name`` renames the SESSION dir in schema form, not ``runs/<slug>``,
    so the container's one name-bearing rename lands one level deeper."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    planted = _plant_session_run(
        config, "widgets", "develop-2", f"{SESSION_ID}--the-name"
    )

    ref = _resolve(config, "develop-2")

    assert ref.path == planted
    assert ref.slug == "develop-2"
    assert ref.dir_name == f"{SESSION_ID}--the-name"
    assert ref.rel_path == f"widgets/_runs/develop-2/{SESSION_ID}--the-name"


def test_the_resolver_and_the_picker_quote_the_same_session(config):
    """The failure the newest-session rule exists to prevent: if the two halves
    disambiguated differently, the candidate list and the fleet row would quote
    different directories of one run."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    _plant_session_run(config, "widgets", "develop-2", SESSION_ID)
    newest = _plant_session_run(config, "widgets", "develop-2", LATER_SESSION_ID)

    listed = [ref for ref in workrepo.run_dirs(config) if ref.slug == "develop-2"]
    resolved = _resolve(config, "develop-2")

    assert len(listed) == 1
    assert resolved.path == listed[0].path == newest
    assert resolved.rel_path == listed[0].rel_path


def test_a_group_holding_several_sessions_says_which_one_it_chose_once(
        config, caplog, announcing):
    """Two launches on one target are two records of ONE identity (D22), so the
    slug is not ambiguous the way two ``<slug>--<name>`` dirs are — but which
    launch is being read is news, and a session id addresses a specific one."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    _plant_session_run(config, "widgets", "develop-2", SESSION_ID)
    _plant_session_run(config, "widgets", "develop-2", LATER_SESSION_ID)

    with caplog.at_level("INFO", logger="lmer_platform.workrepo"):
        _resolve(config, "develop-2")
        said = [r.getMessage() for r in caplog.records
                if "platform_run_dir_sessions" in r.getMessage()]
        caplog.clear()
        _resolve(config, "develop-2")
        again = [r.getMessage() for r in caplog.records
                 if "platform_run_dir_sessions" in r.getMessage()]

    assert len(said) == 1, "the choice between two records must not be silent"
    assert LATER_SESSION_ID in said[0]
    assert again == [], "a read path polled every few seconds says this once"


def test_a_single_session_group_resolves_quietly(config, caplog, announcing):
    """The ordinary schema-form run is not news: one launch, one record."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    _plant_session_run(config, "widgets", "develop-2", SESSION_ID)

    with caplog.at_level("INFO", logger="lmer_platform.workrepo"):
        assert _resolve(config, "develop-2") is not None

    assert [r.getMessage() for r in caplog.records
            if "platform_run_dir" in r.getMessage()] == []


def test_resolve_run_dir_requires_a_state_file_in_the_session_dir(config):
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    _plant_session_run(config, "widgets", "develop-2", SESSION_ID, state=False)

    assert _resolve(config, "develop-2") is None


def test_resolve_run_dir_never_reads_the_grouping_level_as_a_record(config):
    """``_runs/<slug>/`` holds nothing (D22); a state file left there by hand is
    not this run's record."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    group = config.mirror_path / "widgets" / "_runs" / "develop-2"
    group.mkdir(parents=True)
    (group / "state.yaml").write_text("schema: 1\nslug: develop-2\n",
                                      encoding="utf-8")

    assert _resolve(config, "develop-2") is None


def test_resolve_run_dir_skips_the_seed_dir_of_a_session_being_written(config):
    """``_runs/<slug>/.new-<session-id>-*/`` is a directory mid-rename."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    seed = (config.mirror_path / "widgets" / "_runs" / "develop-2"
            / f".new-{SESSION_ID}-a1b2")
    seed.mkdir(parents=True)
    (seed / "state.yaml").write_text("schema: 1\n", encoding="utf-8")

    assert _resolve(config, "develop-2") is None


def test_resolve_run_dir_refuses_a_session_dir_symlinked_out_of_the_mirror(config):
    """Resolving first, then re-checking containment: the segments are innocent
    and the path still leaves the mirror."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    escaped = _plant_escape_target(config)
    group = config.mirror_path / "widgets" / "_runs" / "develop-2"
    group.mkdir(parents=True)
    (group / SESSION_ID).symlink_to(escaped, target_is_directory=True)
    assert (group / SESSION_ID / "state.yaml").is_file(), (
        "the symlink has to reach the target for this to prove anything"
    )

    assert _resolve(config, "develop-2") is None


@pytest.mark.parametrize("slug", [
    "../../elsewhere/runs/secret", "..", "escape*", "**", "[", "a/b",
])
def test_a_schema_form_lookup_never_globs_or_leaves_the_mirror(config, slug):
    """The identity is index-fed, so a hand-edited entry reaches this composition
    without passing any request-path guard. The schema-form lookup joins the slug
    as a path segment and re-checks containment; it never becomes a pattern, so
    ``**`` cannot take the fleet view down with a ``ValueError`` either."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    _plant_session_run(config, "widgets", "escape", SESSION_ID)
    escaped = _plant_escape_target(config)
    assert (escaped / "state.yaml").is_file()

    assert _resolve(config, slug) is None


def test_resolve_run_dir_follows_a_schema_form_run_that_vacated_the_slug(config):
    """A release changes slug after discovering the version; ``reslugged_from``
    is the explicit history, and in schema form it is recorded in a session."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    planted = _plant_session_run(
        config, "widgets", "release-widgets-v1.2.3", SESSION_ID,
        extra={"reslugged_from": ["release-widgets"]},
    )

    ref = _resolve(config, "release-widgets")

    assert ref is not None
    assert ref.path == planted
    assert ref.slug == "release-widgets", "the requested identity is kept"
    assert ref.dir_name == SESSION_ID
    assert ref.rel_path == f"widgets/_runs/release-widgets-v1.2.3/{SESSION_ID}"


def test_a_terminal_schema_form_reslug_does_not_keep_the_seed_address(config):
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    _plant_session_run(
        config, "widgets", "release-widgets-v1.2.3", SESSION_ID,
        extra={"reslugged_from": ["release-widgets"], "status": "complete"},
    )

    assert _resolve(config, "release-widgets") is None


def test_the_registry_is_read_only_when_the_path_derived_shapes_miss(config,
                                                                     monkeypatch):
    """A legacy tree and a host bucket compose their own path (D9), so the common
    case never pays for the manifest; a flat project has no other answer."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    _plant_session_run(config, "widgets", "develop-2", SESSION_ID)
    _plant_session_run(config, "git.example.com/acme/unnamed", "review-mr-3",
                       SESSION_ID)
    loads = []
    real_load = workrepo.load_registry
    monkeypatch.setattr(
        workrepo, "load_registry",
        lambda root: (loads.append(root), real_load(root))[1],
    )

    assert workrepo.resolve_run_dir(
        config, "gitlab.example.com", "agents/global", "develop-1"
    ) is not None
    assert workrepo.resolve_run_dir(
        config, "git.example.com", "acme/unnamed", "review-mr-3"
    ) is not None
    assert loads == [], "the path says where these two live"

    assert _resolve(config, "develop-2") is not None
    assert loads, "a flat short name is only discoverable through the registry"


# --- multi-remote projects (D8: the registry's UNION) ------------------------
#
# ``remotes:`` exists for the project that is reachable at more than one URL,
# and every WRITER resolves across the union — ``work_repo.utils._schema_layout``
# through ``resolve_project_dir`` and ``api._plexus_project_dir`` through
# ``Registry.lookup``. A launch through a secondary remote therefore writes its
# record under the SAME short name, and a resolver that inverted ``repo:`` alone
# reported that run absent. The WALK keys what it lists on ``repo:`` and so does
# the spawn: ``spawn.derive_run_identity`` folds the launch URL to the project's
# primary remote (tests/test_platform_spawn.py), so one directory has one
# identity key. The union stays on this side for the identities that fold does
# not produce — see ``_RegistryIdentities.names_for``.

#: The same project as :data:`FIXTURE_REPO`, reached through its mirror.
FIXTURE_MIRROR_REPO = "https://github.com/acme/widgets.git"
FIXTURE_MIRROR_IDENTITY = ("github.com", "acme/widgets")


def test_resolve_run_dir_finds_a_project_reached_through_a_secondary_remote(
        config):
    """The reviewer's case (!257 discussion 6): the record lands under the short
    name whichever remote the launch used, so the identity the tracked row
    carries is the mirror's — and that used to resolve to nothing."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {
        "widgets": {"repo": FIXTURE_REPO, "remotes": [FIXTURE_MIRROR_REPO]},
    })
    planted = _plant_session_run(config, "widgets", "review-mr-1", SESSION_ID)

    ref = _resolve(config, "review-mr-1", FIXTURE_MIRROR_IDENTITY)

    assert ref is not None, "a run launched through the mirror must not read absent"
    assert ref.path == planted
    assert ref.rel_path == f"widgets/_runs/review-mr-1/{SESSION_ID}"


def test_the_primary_remote_still_resolves_the_same_project(config):
    """The union is an addition, not a move: the ``repo:`` identity answers
    exactly as it did before ``remotes:`` was consulted at all."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {
        "widgets": {"repo": FIXTURE_REPO, "remotes": [FIXTURE_MIRROR_REPO]},
    })
    planted = _plant_session_run(config, "widgets", "review-mr-1", SESSION_ID)

    ref = _resolve(config, "review-mr-1")

    assert ref is not None and ref.path == planted


def test_a_run_reached_through_a_mirror_is_still_listed_under_the_primary(config):
    """Both directions of one project's identity, pinned together: the walk keys
    what it LISTS on ``repo:`` (``_project_identity``), which is now also the
    identity a launch through either remote is tracked under — the launch URL is
    folded to the primary where the identity is derived
    (``spawn.derive_run_identity``), so one directory answers to one key. The
    union stays on the RESOLVE side for the identities that fold does not
    produce: a row tracked by a build that predates it, or a run adopted by hand
    under a secondary's spelling. That is the second assertion here."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {
        "widgets": {"repo": FIXTURE_REPO, "remotes": [FIXTURE_MIRROR_REPO]},
    })
    planted = _plant_session_run(config, "widgets", "review-mr-1", SESSION_ID)

    listed = _by_slug(config)["review-mr-1"]

    assert (listed.host, listed.project) == FIXTURE_IDENTITY, (
        "the walk's keying is unchanged by this fix"
    )
    assert _resolve(config, "review-mr-1", FIXTURE_MIRROR_IDENTITY).path == planted


def test_a_repoless_entry_answers_to_no_remote_at_all(config):
    """D19: a repoless project contributes no keys to the union. It is addressed
    by name, so no identity lookup may ever land in its tree — including the one
    a reader might compose from the short name."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {
        "notes": {"description": "no repo of its own"},
        "widgets": {"repo": FIXTURE_REPO, "remotes": [FIXTURE_MIRROR_REPO]},
    })
    _plant_session_run(config, "notes", "develop-9", SESSION_ID)

    assert _resolve(config, "develop-9", ("gitlab.example.com", "acme/notes")) is None
    assert _resolve(config, "develop-9", FIXTURE_MIRROR_IDENTITY) is None
    assert _resolve(config, "develop-9") is None


def test_an_unregistered_identity_still_falls_through_to_its_host_bucket(config):
    """D9: a repo nobody has named is not in the union under any remote, so the
    union-aware lookup leaves it exactly where it was — the ``{host}/{project}``
    bucket its own remote derives."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {
        "widgets": {"repo": FIXTURE_REPO, "remotes": [FIXTURE_MIRROR_REPO]},
    })
    planted = _plant_session_run(
        config, "git.example.com/acme/unnamed", "review-mr-3", SESSION_ID
    )

    ref = workrepo.resolve_run_dir(
        config, "git.example.com", "acme/unnamed", "review-mr-3"
    )

    assert ref is not None and ref.path == planted
    assert ref.rel_path == (
        f"git.example.com/acme/unnamed/_runs/review-mr-3/{SESSION_ID}"
    )


def test_a_schema_form_resolve_survives_an_unusable_manifest(config, caplog,
                                                             announcing):
    """A broken ``_lmer.yaml`` is the validator's problem. This read path loses
    the runs whose identity that file held, says so once, and nothing else."""
    workrepo.ensure_clone(config)
    (config.mirror_path / "_lmer.yaml").write_text("projects: [", encoding="utf-8")
    _plant_session_run(config, "widgets", "develop-2", SESSION_ID)

    with caplog.at_level("WARNING", logger="lmer_platform.workrepo"):
        assert _resolve(config, "develop-2") is None
        assert workrepo.resolve_run_dir(
            config, "gitlab.example.com", "agents/global", "develop-1"
        ) is not None, "the legacy tree still resolves"

    said = [r.getMessage() for r in caplog.records
            if "platform_mirror_manifest_unusable" in r.getMessage()]
    assert len(said) == 1


def test_resolving_never_writes_into_the_mirror(config):
    """The mirror is a read-only checkout: a state file this daemon cannot parse
    must not be moved aside for post-mortem (``run_state.load_state`` does that,
    which is why nothing here goes through it) — the next ``reset --hard`` would
    trip over the backup.

    The slug asked for is absent on purpose, so every fallback runs and every
    state file in the project is read.
    """
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    session = _plant_session_run(config, "widgets", "develop-2", SESSION_ID)
    (session / "state.yaml").write_text("slug: [unclosed\n", encoding="utf-8")
    before = _mirror_contents(config)

    assert _resolve(config, "no-such-run") is None

    assert _mirror_contents(config) == before
    assert not list(config.mirror_path.rglob("*.bad-*"))


def test_every_schema_form_run_listed_resolves_under_the_key_it_is_listed_with(
        config):
    """The picker and the resolver are one contract in schema form too: adopting
    a candidate tracks it under the key shown, and every verb afterwards resolves
    that key."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    _plant_session_run(config, "widgets", "develop-2", SESSION_ID)
    _plant_session_run(config, "widgets", "develop-2", LATER_SESSION_ID)
    _plant_session_run(config, "widgets", "review-mr-9", f"{SESSION_ID}--the-name")
    _plant_session_run(config, "git.example.com/acme/unnamed", "review-mr-3",
                       SESSION_ID)

    listed = workrepo.run_dirs(config)

    assert {ref.slug for ref in listed} == {
        "develop-1", "develop-2", "review-mr-9", "review-mr-3"
    }
    for ref in listed:
        found = workrepo.resolve_run_dir(config, ref.host, ref.project, ref.slug)
        assert found is not None and found.path == ref.path, (
            f"{ref.slug} is offered but does not resolve back to {ref.rel_path}"
        )
        assert found.rel_path == ref.rel_path


def test_a_legacy_run_resolves_exactly_as_before_beside_a_schema_project(config):
    """Co-existence (D16/D20): teaching the resolver the second shape must not
    move the legacy tree's answer by a byte."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    _plant_session_run(config, "widgets", "develop-2", SESSION_ID)
    _plant_run_dir(config, f"{SLUG}--the-name")

    bare = workrepo.resolve_run_dir(
        config, "gitlab.example.com", "agents/global", "develop-1"
    )
    named = workrepo.resolve_run_dir(
        config, "gitlab.example.com", "agents/global", SLUG
    )

    assert bare.dir_name is None
    assert bare.rel_path == "gitlab.example.com/agents/global/runs/develop-1"
    assert named.dir_name == f"{SLUG}--the-name"
    assert named.rel_path == (
        f"gitlab.example.com/agents/global/runs/{SLUG}--the-name"
    )
# --- which session addresses the group (!256, discussion 3) -----------------
#
# "Newest session by directory NAME" is only start order while every
# state-bearing directory in the group is named in the ``LMER_SESSION_ID`` form
# — and nothing enforces that: this same schema registers the non-conforming
# case as a WARNING (``record.session-id-malformed``), so the fleet view meets
# it rather than assuming it away. A hand-made copy sorts after ``2026…``
# (``b`` > ``2``), so it used to win the group; the single ref then addressed
# the copy and the fleet row read ``complete`` for a run that was live.

#: The reviewer's tree: an old finished launch, the live one, and a copy taken
#: by hand whose name is in no form at all.
EARLY_SESSION_ID = "20260101-000000-1-1"
LIVE_SESSION_ID = "20260825-060108-2-19758"
OUT_OF_FORM_DIR = "backup-before-i-broke-it"


def _plant_reviewers_tree(config):
    """``widgets/_runs/develop-1/`` with two launches and one hand-made copy."""
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    _plant_session_run(config, "widgets", "develop-1", EARLY_SESSION_ID,
                       extra={"status": "complete"})
    live = _plant_session_run(config, "widgets", "develop-1", LIVE_SESSION_ID,
                              extra={"status": "in-progress"})
    _plant_session_run(config, "widgets", "develop-1", OUT_OF_FORM_DIR,
                       extra={"status": "complete"})
    return live


def _schema_form_refs(config):
    """The refs from the flat project dir — the legacy fixture also has a
    ``develop-1``, and it is a different run under a different identity."""
    return [ref for ref in workrepo.run_dirs(config) if ref.project == "acme/widgets"]


def _mirror_snapshot(config):
    """Every path under the mirror with its kind, size and mtime (not ``.git``)."""
    snapshot = {}
    for path in sorted(config.mirror_path.rglob("*")):
        rel = path.relative_to(config.mirror_path)
        if ".git" in rel.parts:
            continue
        info = path.lstat()
        snapshot[str(rel)] = (path.is_dir(), info.st_size, info.st_mtime_ns)
    return snapshot


def test_run_dirs_pick_the_newest_session_id_and_ignore_a_dir_that_is_not_one(
        config, caplog, announcing):
    """The reviewer's tree: the live launch wins, the copy is ignored.

    Ordering by name is only meaningful among names the schema gives an order —
    the id form. A directory in no form has no place in that comparison, so it
    is passed over rather than crowned, and said once: a run reported
    ``complete`` while it is running is exactly the disappearance this listing
    must not cause.
    """
    workrepo.ensure_clone(config)
    live = _plant_reviewers_tree(config)

    with caplog.at_level("INFO", logger="lmer_platform.workrepo"):
        listed = _schema_form_refs(config)

    assert len(listed) == 1, "one run identity is one candidate"
    assert listed[0].path == live
    assert listed[0].rel_path == f"widgets/_runs/develop-1/{LIVE_SESSION_ID}"
    assert yaml.safe_load(
        (listed[0].path / "state.yaml").read_text(encoding="utf-8")
    )["status"] == "in-progress", "the addressed record is the live one"
    said = [r.getMessage() for r in caplog.records
            if "platform_session_dir_not_in_form" in r.getMessage()]
    assert len(said) == 1 and OUT_OF_FORM_DIR in said[0]


def test_run_dirs_keep_a_group_whose_sessions_are_all_out_of_form(config, caplog,
                                                                  announcing):
    """Losing the run from the fleet view would be worse than the defect fixed.

    With no id-form child there is no schema-given order, so the group falls
    back to the newest directory MTIME — a stat, never a read, so the walk keeps
    costing what it did — and says so, because that ordering is this module's
    own choice and not something the tree promised.
    """
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    # Name order would pick ``restored-copy``; mtime picks the other one.
    older = _plant_session_run(config, "widgets", "develop-2", "restored-copy")
    newer = _plant_session_run(config, "widgets", "develop-2", OUT_OF_FORM_DIR)
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

    with caplog.at_level("INFO", logger="lmer_platform.workrepo"):
        listed = _schema_form_refs(config)

    assert [ref.path for ref in listed] == [newer], "the run is still offered"
    said = [r.getMessage() for r in caplog.records
            if "platform_session_dir_undated" in r.getMessage()]
    assert len(said) == 1 and "develop-2" in said[0]


def test_run_dirs_prefer_any_session_id_to_the_newest_thing_on_disk(config):
    """The fallback is for a group with NO id-form child, and only that: a copy
    made after the launch it copied is still not the record."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    session = _plant_session_run(config, "widgets", "develop-2", SESSION_ID)
    copy = _plant_session_run(config, "widgets", "develop-2", OUT_OF_FORM_DIR)
    os.utime(session, (1_000_000, 1_000_000))
    os.utime(copy, (2_000_000, 2_000_000))

    assert [ref.path for ref in _schema_form_refs(config)] == [session]


def test_run_dirs_order_opaque_uids_by_recorded_start_time(config):
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}}, schema=2)
    group = config.mirror_path / "widgets" / "_runs" / "develop-2"
    lexically_later = _plant_session_run(
        config, "widgets", "develop-2", "f" * 32,
        extra={"schema": 2, "session": {"started_at": "2026-01-01T00:00:00+00:00"}},
    )
    chronologically_later = _plant_session_run(
        config, "widgets", "develop-2", "0" * 32,
        extra={"schema": 2, "session": {"started_at": "2026-02-01T00:00:00+00:00"}},
    )
    os.utime(lexically_later, (3_000_000, 3_000_000))
    os.utime(chronologically_later, (1_000_000, 1_000_000))

    assert workrepo._newest_session(group) == chronologically_later


def test_opaque_uid_sorting_falls_back_from_bad_started_at_to_state_created(config):
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}}, schema=2)
    group = config.mirror_path / "widgets" / "_runs" / "develop-2"
    created_later = _plant_session_run(
        config, "widgets", "develop-2", "f" * 32,
        extra={
            "schema": 2,
            "created": "2026-03-01T00:00:00+00:00",
            "session": {"started_at": "not-a-timestamp"},
        },
    )
    mtime_later = _plant_session_run(
        config, "widgets", "develop-2", "0" * 32,
        extra={
            "schema": 2,
            "created": "2026-02-01T00:00:00+00:00",
            "session": {},
        },
    )
    os.utime(created_later, (1_000_000, 1_000_000))
    os.utime(mtime_later, (3_000_000, 3_000_000))

    assert workrepo._newest_session(group) == created_later


def test_opaque_uid_sorting_treats_a_disappearing_record_as_oldest(
        config, monkeypatch):
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}}, schema=2)
    group = config.mirror_path / "widgets" / "_runs" / "develop-2"
    present = _plant_session_run(
        config, "widgets", "develop-2", "0" * 32,
        extra={"schema": 2, "session": {"started_at": "2026-02-01T00:00:00+00:00"}},
    )
    disappearing = _plant_session_run(
        config, "widgets", "develop-2", "f" * 32,
        extra={"schema": 2, "session": {"started_at": "2026-03-01T00:00:00+00:00"}},
    )
    real_sort_key = workrepo.session_record.sort_key

    def vanish_before_sort(path):
        if path == disappearing and path.exists():
            (path / "state.yaml").unlink()
            path.rmdir()
        return real_sort_key(path)

    monkeypatch.setattr(workrepo.session_record, "sort_key", vanish_before_sort)

    assert workrepo._newest_session(group) == present


def test_run_dirs_never_write_into_the_mirror(config):
    """The mirror is a read-only checkout (:func:`workrepo._recorded_slug`): a
    listing that stamped anything would be a local modification for the next
    ``reset --hard`` to trip over. Ordering by mtime reads it, never sets it."""
    workrepo.ensure_clone(config)
    _plant_reviewers_tree(config)
    _plant_session_run(config, "widgets", "develop-2", "restored-copy")

    before = _mirror_snapshot(config)
    workrepo.run_dirs(config)

    assert _mirror_snapshot(config) == before


# --- the session id a record is addressed by (issue #353, D22) ---------------
#
# ``LMER_SESSION_ID`` is the one thing that decides which record a launch writes
# in: the container mints one at startup only when the host injected none
# (``clone_and_exec.main`` -> ``mint_session_id``). So the verbs that continue a
# particular record (``lmer_platform.resume``, ``lmer_platform.answer``) have to
# be able to ask "which session is this ref addressed by" — and the two rules
# that answer it, what counts as a session record and what the ``--<name>``
# rename does to a dir name, already live here rather than in either verb.


def test_record_session_id_names_the_launch_of_a_schema_form_record(config):
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    _plant_session_run(config, "widgets", "develop-2", SESSION_ID)

    assert workrepo.record_session_id(_resolve(config, "develop-2")) == SESSION_ID


def test_record_session_id_is_the_id_half_of_a_named_session_dir(config):
    """``<sid>`` and ``<sid>--<name>`` are the same session and the id is
    ``<sid>``. Answering with the directory name would work today by accident and
    compose ``<sid>--<name>--<name>`` the moment anything built the record's
    address back out of it."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    _plant_session_run(config, "widgets", "develop-2", f"{SESSION_ID}--the-name")

    ref = _resolve(config, "develop-2")

    assert ref.dir_name == f"{SESSION_ID}--the-name"
    assert workrepo.record_session_id(ref) == SESSION_ID


def test_record_session_id_is_none_for_a_legacy_run_dir(config):
    """One shared record per target, read in place and never restructured: no
    launch addresses it, so there is nothing to hand a resume."""
    workrepo.ensure_clone(config)
    _plant_run_dir(config, SLUG)

    ref = workrepo.resolve_run_dir(config, "gitlab.example.com", "agents/global", SLUG)

    assert ref is not None, "the legacy shape must still resolve"
    assert workrepo.record_session_id(ref) is None


def test_record_session_id_is_none_for_an_unmigrated_group_level_record(config):
    """A tree adopted before its runs were migrated holds
    ``_runs/<slug>/state.yaml`` — the record is the grouping dir itself
    (``work_repo.run_state._creation_address``), addressed by the slug rather
    than by a launch.

    The ref is built by hand because this shape is not one the walk lists at all
    (a group with no session child yields nothing), and the helper still has to
    answer for it: the alternative answer is a run's SLUG travelling as a session
    id."""
    workrepo.ensure_clone(config)
    group = config.mirror_path / "widgets" / "_runs" / "develop-2"
    group.mkdir(parents=True)
    (group / "state.yaml").write_text(
        "schema: 1\nslug: develop-2\nstatus: in-progress\n", encoding="utf-8"
    )

    ref = workrepo.RunDirRef(
        host=FIXTURE_IDENTITY[0],
        project=FIXTURE_IDENTITY[1],
        slug="develop-2",
        path=group,
        address="widgets/_runs/develop-2",
    )

    assert workrepo.record_session_id(ref) is None


def test_record_session_id_answers_a_dir_name_that_is_not_in_the_form(
        config, caplog, announcing):
    """``_newest_session`` addresses a group whose children are in no form at all
    rather than dropping the run from the fleet view, so a resolved record CAN sit
    at a name the session-id grammar does not describe. That name is still what
    addresses it, and refusing to name it would leave the operator unable to
    continue the record the view is showing them."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": FIXTURE_REPO}})
    _plant_session_run(config, "widgets", "develop-2", OUT_OF_FORM_DIR)

    ref = _resolve(config, "develop-2")

    assert ref.dir_name == OUT_OF_FORM_DIR
    assert workrepo.record_session_id(ref) == OUT_OF_FORM_DIR


# --- the execution-uid lookup (A2.workrepo-uid, spec §6.1, review N6) --------
#
# A schema-2 run is keyed by its execution uid, and the group directory is only
# a label. So a uid row must resolve to ITS record — not to the newest record in
# the group the uid's slug happens to name, and not to a record that a different
# group holds.

UID_A = "a" * 32
UID_B = "b" * 32
UID_C = "c" * 32
#: A schema-2 project whose identity has no endpoint port, so FIXTURE_IDENTITY
#: (which carries none) addresses it.
UID_REPO = "ssh://git@gitlab.example.com/acme/widgets.git"


def _plant_uid(config, project_dir, slug, uid, started_at, *, name=None):
    dir_name = f"{uid}--{name}" if name else uid
    return _plant_session_run(
        config, project_dir, slug, dir_name,
        extra={"schema": 2, "session": {"started_at": started_at}},
    )


def test_resolve_run_dir_by_uid_finds_each_record_of_one_group(config):
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": UID_REPO}}, schema=2)
    first = _plant_uid(config, "widgets", "develop-2", UID_A, "2026-01-01T00:00:00+00:00")
    second = _plant_uid(config, "widgets", "develop-2", UID_B, "2026-02-01T00:00:00+00:00")

    ref_a = workrepo.resolve_run_dir(config, *FIXTURE_IDENTITY, uid=UID_A)
    ref_b = workrepo.resolve_run_dir(config, *FIXTURE_IDENTITY, uid=UID_B)

    assert ref_a is not None and ref_a.path == first
    assert ref_b is not None and ref_b.path == second
    assert ref_a.uid == UID_A and ref_b.uid == UID_B
    assert ref_a.slug == ref_b.slug == "develop-2"


def test_resolve_run_dir_by_uid_crosses_groups(config):
    """A folded or reslugged record still resolves when only its uid is known."""
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": UID_REPO}}, schema=2)
    _plant_uid(config, "widgets", "develop-2", UID_A, "2026-01-01T00:00:00+00:00")
    other_group = _plant_uid(config, "widgets", "develop-9", UID_B, "2026-02-01T00:00:00+00:00")

    ref = workrepo.resolve_run_dir(
        config, *FIXTURE_IDENTITY, "develop-2", uid=UID_B
    )

    assert ref is not None and ref.path == other_group
    assert ref.slug == "develop-9"


def test_resolve_run_dir_unknown_uid_never_falls_back(config):
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": UID_REPO}}, schema=2)
    _plant_uid(config, "widgets", "develop-2", UID_A, "2026-01-01T00:00:00+00:00")

    assert workrepo.resolve_run_dir(config, *FIXTURE_IDENTITY, uid=UID_C) is None


def test_uid_lookup_scans_once_per_mirror_revision(config, monkeypatch):
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": UID_REPO}}, schema=2)
    _plant_uid(config, "widgets", "develop-2", UID_A, "2026-01-01T00:00:00+00:00")
    revision = {"sha": "a" * 40}
    monkeypatch.setattr(
        workrepo, "mirror_status",
        lambda _config: workrepo.MirrorStatus(
            present=True, last_pull_ok=True, head_sha=revision["sha"]
        ),
    )
    original = workrepo._build_uid_index
    builds = []

    def counted(*args):
        builds.append(revision["sha"])
        return original(*args)

    monkeypatch.setattr(workrepo, "_build_uid_index", counted)

    assert workrepo.resolve_run_dir(config, *FIXTURE_IDENTITY, uid=UID_A) is not None
    assert workrepo.resolve_run_dir(config, *FIXTURE_IDENTITY, uid=UID_A) is not None
    revision["sha"] = "b" * 40
    assert workrepo.resolve_run_dir(config, *FIXTURE_IDENTITY, uid=UID_A) is not None

    assert builds == ["a" * 40, "b" * 40]


def test_group_records_lists_a_group_in_start_order(config):
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": UID_REPO}}, schema=2)
    # The lexically LARGER uid started EARLIER, so name order (UID_A first) and
    # start order (UID_B first) disagree: a name-order sort fails here.
    later = _plant_uid(config, "widgets", "develop-2", UID_A, "2026-02-01T00:00:00+00:00")
    earlier = _plant_uid(config, "widgets", "develop-2", UID_B, "2026-01-01T00:00:00+00:00")
    assert sorted([UID_A, UID_B]) == [UID_A, UID_B]

    refs = workrepo.group_records(config, *FIXTURE_IDENTITY, "develop-2")

    assert [ref.path for ref in refs] == [earlier, later]
    assert [ref.uid for ref in refs] == [UID_B, UID_A]


def test_group_records_empty_for_an_unknown_slug(config):
    workrepo.ensure_clone(config)
    _plant_manifest(config, {"widgets": {"repo": UID_REPO}}, schema=2)
    assert workrepo.group_records(config, *FIXTURE_IDENTITY, "nope") == []
