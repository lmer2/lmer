"""Session identity reservation and launch release (#519, #636 item 22).

A launch reserves its session id with the control token's exclusive create
before any other per-session artifact exists, so a clashing candidate is given
up without touching the session that holds it. Launches run for real against
the stub ``lmer`` from :mod:`tests.test_platform_spawn`.
"""

import itertools
import os
import select
import signal
import sys
import threading

import pytest

from lmer_platform import launch_guard, projects, registry, spawn
from tests.test_platform_spawn import (  # noqa: F401 — fixtures
    _clean_lmer_env, _no_user_harnesses, config, fake_lmer, platform_root, request_for,
    wait_for,
)
from tests.timing_budgets import JOIN, SETTLE

FRESH = "s-20990101T000000-0123456789ab"


def _target(item):
    return f"https://gitlab.example.com/group/project/-/work_items/{item}"


def _ids(monkeypatch, *candidates):
    """Make ``new_session_id`` yield *candidates*, then fail loudly if asked again."""
    drawn = []
    source = iter(candidates)

    def draw():
        drawn.append(next(source))
        return drawn[-1]

    monkeypatch.setattr(registry, "new_session_id", draw)
    return drawn


def _tree(root):
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def _kill(*pids):
    for pid in pids:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_a_reserved_candidate_is_skipped_and_its_holder_left_untouched(config, monkeypatch):
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")
    incumbent = spawn.spawn_session(config, request_for(target=_target(1)))
    second = None
    try:
        assert wait_for(lambda: incumbent.log_path.is_file()
                        and b"fake lmer started" in incumbent.log_path.read_bytes(), SETTLE)
        token = spawn.token_file_for(incumbent.session_id).read_bytes()
        entry = registry.session_path(incumbent.session_id).read_bytes()
        log = incumbent.log_path.read_bytes()

        drawn = _ids(monkeypatch, incumbent.session_id, FRESH)
        second = spawn.spawn_session(config, request_for(target=_target(2)))

        assert drawn == [incumbent.session_id, FRESH]
        assert second.session_id == FRESH
        assert spawn.token_file_for(incumbent.session_id).read_bytes() == token
        assert registry.session_path(incumbent.session_id).read_bytes() == entry
        assert incumbent.log_path.read_bytes() == log
        assert spawn.read_control_token(FRESH) not in (None, token.decode())
        assert registry.read_session(FRESH)["pid"] == second.pid
    finally:
        _kill(incumbent.pid, *([second.pid] if second else []))


def test_an_id_whose_session_left_its_log_is_not_reused(config, monkeypatch):
    """A finished session's token goes with its entry; its log stays behind."""
    gone = "s-20990101T000000-aaaaaaaaaaaa"
    scrollback = spawn.log_path_for(gone)
    scrollback.parent.mkdir(parents=True)
    scrollback.write_bytes(b"the finished session's scrollback\n")

    _ids(monkeypatch, gone, FRESH)
    result = spawn.spawn_session(config, request_for())
    try:
        assert result.session_id == FRESH
        assert scrollback.read_bytes() == b"the finished session's scrollback\n"
        assert not spawn.token_file_for(gone).exists()
    finally:
        _kill(result.pid)


def test_every_candidate_taken_is_a_bounded_refusal_that_leaves_nothing(
    config, platform_root, monkeypatch
):
    taken = "s-20990101T000000-bbbbbbbbbbbb"
    token, path = spawn._mint_control_token(taken)
    before = _tree(platform_root)
    drawn = _ids(monkeypatch, *itertools.repeat(taken, spawn._SESSION_ID_ATTEMPTS + 1))

    with pytest.raises(spawn.SpawnError, match=f"{spawn._SESSION_ID_ATTEMPTS} candidates"):
        spawn.spawn_session(config, request_for())

    assert len(drawn) == spawn._SESSION_ID_ATTEMPTS
    assert _tree(platform_root) == before
    assert path.read_text(encoding="utf-8") == token
    assert not registry.list_sessions(live_only=False)


def test_concurrent_reservations_never_hold_one_id_twice(platform_root, monkeypatch):
    """Both threads draw the same candidate before either creates its token."""
    same = "s-20990101T000000-cccccccccccc"
    barrier = threading.Barrier(2, timeout=JOIN)
    lock = threading.Lock()
    calls = itertools.count()

    def draw():
        with lock:
            call = next(calls)
        if call < 2:
            barrier.wait()
            return same
        return f"s-20990101T000000-{call:012x}"

    monkeypatch.setattr(registry, "new_session_id", draw)
    results, errors = [], []

    def reserve():
        try:
            results.append(spawn._reserve_session_identity())
        except BaseException as exc:  # surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=reserve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(JOIN)
    assert not errors
    ids = [session_id for session_id, _token, _path in results]
    assert len(ids) == 2 and len(set(ids)) == 2
    assert ids.count(same) == 1
    for session_id, token, path in results:
        assert path.read_text(encoding="utf-8") == token
        assert path == spawn.token_file_for(session_id)


def test_a_failure_before_the_process_starts_removes_only_its_own_token(
    config, monkeypatch
):
    incumbent = "s-20990101T000000-dddddddddddd"
    token, path = spawn._mint_control_token(incumbent)
    _ids(monkeypatch, FRESH)

    def no_pty():
        raise OSError("injected: out of ptys")

    monkeypatch.setattr(spawn.pty, "openpty", no_pty)
    with pytest.raises(OSError, match="out of ptys"):
        spawn.spawn_session(config, request_for())
    assert not spawn.token_file_for(FRESH).exists()
    assert path.read_text(encoding="utf-8") == token


def test_an_unreadable_artifact_is_a_spawn_error_that_removes_the_minted_token(
    config, monkeypatch
):
    """Path.exists() raises on EACCES; the token minted for the id goes too."""

    class Unreadable:
        def exists(self):
            raise PermissionError(13, "injected: unreadable uploads dir")

    monkeypatch.setattr(spawn, "_session_artifacts", lambda _session_id: (Unreadable(),))
    _ids(monkeypatch, FRESH)
    with pytest.raises(spawn.SpawnError, match="unreadable uploads dir"):
        spawn.spawn_session(config, request_for())
    assert not spawn.token_file_for(FRESH).exists()
    assert not registry.list_sessions(live_only=False)


def test_a_launch_guard_gone_before_release_is_a_spawn_error_with_nothing_left(
    config, monkeypatch
):
    """The guard can die (a signal) before the release write; that is a
    failed launch, not a raw BrokenPipeError with an entry."""
    projects.migrate()
    monkeypatch.setattr(spawn, "_FAILED_PUBLICATIONS", {})
    monkeypatch.setattr(spawn, "_UNPUBLISHED_TERM_GRACE", 0)
    published = []

    def publish(register, session_id, pid):
        register()
        assert registry.session_path(session_id).exists()
        published.append((session_id, pid))
        os.kill(pid, signal.SIGKILL)
        # Waits for the exit, which closes the guard's pipe end, but leaves the
        # child unreaped for the launch's own cleanup to collect.
        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)

    with projects.admitting(projects.GENERAL), pytest.raises(spawn.SpawnError, match="launch guard") as refused:
        spawn.spawn_session(
            config, request_for(workspace_id=projects.GENERAL), kind=registry.ASSISTANT_KIND,
            publish_registration=publish,
        )

    assert isinstance(refused.value.__cause__, BrokenPipeError)
    [(session_id, _pid)] = published
    assert not spawn._FAILED_PUBLICATIONS
    assert not registry.session_path(session_id).exists()
    assert not spawn.token_file_for(session_id).exists()
    assert not registry.list_sessions(live_only=False)


@pytest.mark.parametrize(("release", "launched"), [(b"1", True), (None, False)])
def test_the_guard_waits_for_the_parent_however_long_publication_takes(
    monkeypatch, release, launched
):
    """The parent's decision, not a clock, ends the guard's wait (#636 item 22).

    ``select`` reports "not ready" at once, which is what a deadline running
    out under load looks like; the guard must still wait for the write, or
    for the parent closing its end, which refuses.
    """
    monkeypatch.setattr(select, "select", lambda *_args: ([], [], []))
    execs = []
    monkeypatch.setattr(os, "execvp", lambda file, args: execs.append(args))
    read_end, write_end = os.pipe()
    error_read, error_write = os.pipe()
    monkeypatch.setattr(sys, "argv", ["launch_guard", str(read_end), str(error_write), "assistant"])
    results = []
    guard = threading.Thread(target=lambda: results.append(launch_guard.main()))
    guard.start()
    try:
        if release is not None:
            os.write(write_end, release)
    finally:
        os.close(write_end)
        guard.join(JOIN)
        os.close(error_read)
        os.close(error_write)
    assert not guard.is_alive()
    assert execs == ([["assistant"]] if launched else [])
    assert results == [None if launched else 1]
