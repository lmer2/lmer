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
