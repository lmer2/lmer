"""Tests for slack_chat.registry — the host-side live-session registry that
lets the Slack listener detect an lmer already attached to a thread and avoid
connecting a second one (issue #74).

Every test points ``REGISTRY_DIR`` at a per-test temp dir (autouse fixture), so
nothing here touches the real ``~/.lmer/slack-sessions``.
"""

import json
import os
import subprocess

import pytest

from slack_chat import registry

CHANNEL = "C0123ABC"
THREAD_TS = "1700000000.123456"
PERMALINK = "https://x.slack.com/archives/C0123ABC/p1700000000123456"


@pytest.fixture(autouse=True)
def _tmp_registry(tmp_path, monkeypatch):
    """Point the registry at a temp dir for every test in this module."""
    monkeypatch.setattr(registry, "REGISTRY_DIR", str(tmp_path))
    return tmp_path


class TestRegisterAndQuery:
    def test_register_then_connected_for_live_pid(self):
        # The current process is alive, so an entry recorded under our own PID
        # reads back as connected.
        registry.register(CHANNEL, THREAD_TS, permalink=PERMALINK)
        assert registry.is_thread_connected(CHANNEL, THREAD_TS) is True

    def test_not_connected_without_entry(self):
        assert registry.is_thread_connected(CHANNEL, THREAD_TS) is False

    def test_deregister_removes_entry(self):
        registry.register(CHANNEL, THREAD_TS)
        assert registry.is_thread_connected(CHANNEL, THREAD_TS) is True
        registry.deregister(CHANNEL, THREAD_TS)
        assert registry.is_thread_connected(CHANNEL, THREAD_TS) is False

    def test_deregister_is_idempotent(self):
        # No entry present — deregister must be a silent no-op, not an error.
        registry.deregister(CHANNEL, THREAD_TS)
        registry.deregister(CHANNEL, THREAD_TS)

    def test_entry_stores_pid_channel_thread_and_permalink(self):
        registry.register(CHANNEL, THREAD_TS, permalink=PERMALINK)
        data = json.loads(registry._entry_path(CHANNEL, THREAD_TS).read_text())
        assert data["pid"] == os.getpid()
        assert data["channel"] == CHANNEL
        assert data["thread_ts"] == THREAD_TS
        assert data["permalink"] == PERMALINK

    def test_register_explicit_pid_is_used(self):
        registry.register(CHANNEL, THREAD_TS, pid=4242)
        data = json.loads(registry._entry_path(CHANNEL, THREAD_TS).read_text())
        assert data["pid"] == 4242

    def test_register_leaves_no_tmp_file(self, _tmp_registry):
        registry.register(CHANNEL, THREAD_TS)
        leftovers = [p.name for p in _tmp_registry.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_threads_are_independent(self):
        registry.register(CHANNEL, THREAD_TS)
        assert registry.is_thread_connected(CHANNEL, "9999999999.000001") is False
        assert registry.is_thread_connected("D999", THREAD_TS) is False


class TestDeregisterOwnership:
    """deregister removes only entries this process owns (review on !126):
    register overwrites unconditionally, so with two manual sessions on one
    thread the first exit must not delete the survivor's entry."""

    def test_foreign_entry_is_left_in_place(self):
        # Entry recorded by "another" (live-or-not doesn't matter) process.
        registry.register(CHANNEL, THREAD_TS, pid=os.getpid() + 1)
        registry.deregister(CHANNEL, THREAD_TS)
        assert registry._entry_path(CHANNEL, THREAD_TS).exists()

    def test_own_entry_is_removed(self):
        registry.register(CHANNEL, THREAD_TS)  # pid defaults to ours
        registry.deregister(CHANNEL, THREAD_TS)
        assert not registry._entry_path(CHANNEL, THREAD_TS).exists()

    def test_corrupt_entry_is_removed(self):
        # A corrupt entry protects nothing — deregister may clean it up.
        path = registry._entry_path(CHANNEL, THREAD_TS)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        registry.deregister(CHANNEL, THREAD_TS)
        assert not path.exists()


class TestStaleEntries:
    def test_dead_pid_reads_as_not_connected(self, monkeypatch):
        registry.register(CHANNEL, THREAD_TS)
        monkeypatch.setattr(registry, "_pid_alive", lambda pid: False)
        assert registry.is_thread_connected(CHANNEL, THREAD_TS) is False

    def test_dead_pid_entry_is_left_in_place_on_read(self, monkeypatch):
        # Reads must never mutate the registry: a read-path unlink could race
        # with a concurrent register() and delete a freshly-written live entry,
        # failing unsafe (MR !102 review). A stale entry is reclaimed by the
        # next register(), not by the read.
        registry.register(CHANNEL, THREAD_TS)
        path = registry._entry_path(CHANNEL, THREAD_TS)
        monkeypatch.setattr(registry, "_pid_alive", lambda pid: False)
        assert registry.is_thread_connected(CHANNEL, THREAD_TS) is False
        assert path.exists(), "a read must not delete the entry it observed"

    def test_register_reclaims_a_stale_entry(self, monkeypatch):
        # Treat only the current process as alive, so a foreign pid is stale.
        monkeypatch.setattr(registry, "_pid_alive", lambda pid: pid == os.getpid())
        registry.register(CHANNEL, THREAD_TS, pid=4242)
        assert registry.is_thread_connected(CHANNEL, THREAD_TS) is False
        # A new session for the same thread overwrites the stale entry, so the
        # thread is never permanently blocked even though the read left it.
        registry.register(CHANNEL, THREAD_TS)
        assert registry.is_thread_connected(CHANNEL, THREAD_TS) is True
        data = json.loads(registry._entry_path(CHANNEL, THREAD_TS).read_text())
        assert data["pid"] == os.getpid()

    def test_corrupt_entry_reads_as_not_connected(self):
        path = registry._entry_path(CHANNEL, THREAD_TS)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not valid json {{{")
        assert registry.is_thread_connected(CHANNEL, THREAD_TS) is False
        # Left in place (not unlinked on read); a later register() overwrites it.
        assert path.exists()

    def test_non_object_entry_reads_as_not_connected(self):
        # Valid JSON but not an object (no .get) must be treated as corrupt.
        path = registry._entry_path(CHANNEL, THREAD_TS)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1, 2, 3]")
        assert registry.is_thread_connected(CHANNEL, THREAD_TS) is False
        assert path.exists()


class TestMissingIds:
    @pytest.mark.parametrize(
        "channel,thread_ts",
        [(None, THREAD_TS), (CHANNEL, None), ("", THREAD_TS), (CHANNEL, ""), (None, None)],
    )
    def test_register_is_noop_without_both_ids(self, channel, thread_ts, _tmp_registry):
        registry.register(channel, thread_ts)
        assert list(_tmp_registry.iterdir()) == []

    @pytest.mark.parametrize(
        "channel,thread_ts",
        [(None, THREAD_TS), (CHANNEL, None), ("", ""), (None, None)],
    )
    def test_is_connected_is_false_without_both_ids(self, channel, thread_ts):
        assert registry.is_thread_connected(channel, thread_ts) is False


class TestPidAlive:
    def test_current_process_is_alive(self):
        assert registry._pid_alive(os.getpid()) is True

    def test_nonpositive_pids_are_not_alive(self):
        assert registry._pid_alive(0) is False
        assert registry._pid_alive(-1) is False

    def test_reaped_child_pid_is_not_alive(self):
        # A child we've already waited on is gone; its PID should not read as
        # alive (barring an immediate, vanishingly unlikely reuse).
        proc = subprocess.Popen(["true"])
        proc.wait()
        assert registry._pid_alive(proc.pid) is False


class TestEntryPathSafety:
    def test_path_separators_are_stripped(self):
        # channel/thread_ts come from a parsed permalink and can't contain a
        # separator, but a crafted value must still never escape the dir.
        path = registry._entry_path("a/b", "c/d")
        assert path.parent == registry._registry_dir()
        assert "/" not in path.name
        assert os.sep not in path.name


class TestRegistryDirDefault:
    def test_defaults_under_lmer_state_dir(self, monkeypatch, tmp_path):
        # With REGISTRY_DIR unset, the directory derives from the lmer state dir.
        monkeypatch.setattr(registry, "REGISTRY_DIR", None)
        monkeypatch.setattr(registry, "lmer_state_dir", lambda: tmp_path)
        assert registry._registry_dir() == tmp_path / "slack-sessions"
