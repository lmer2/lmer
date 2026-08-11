"""Tests for the platform session registry (issue #141, slice M1 / T2).

The properties that matter: liveness comes from the session's own PID (not the
daemon's), ownership is checked on delete, reads tolerate one bad file and never
mutate, stale entries are reclaimed only by an explicit sweep, a bearer token can
never be written into an entry, and the sibling token file leaves with the entry
it belonged to rather than outliving it as an unexplained credential.
"""

import json
import os

import pytest

from lmer_platform import registry, store
from tests.conftest import strip_lmer_env


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


DEAD_PID = 2**22  # far above /proc/sys/kernel/pid_max on normal systems


@pytest.fixture
def live_pid():
    """A PID that certainly exists: this test process."""
    return os.getpid()


# --- ids --------------------------------------------------------------------

def test_new_session_id_is_sortable_and_unique():
    a, b = registry.new_session_id(), registry.new_session_id()
    assert a.startswith("s-")
    assert a != b
    assert registry._SESSION_ID_RE.match(a)


@pytest.mark.parametrize("bad", ["", "../escape", "a/b", "with space", ".hidden", None])
def test_session_path_rejects_unsafe_ids(platform_root, bad):
    """Ids are validated, never sanitized: mangling could collide two sessions."""
    with pytest.raises(registry.RegistryError):
        registry.session_path(bad)


def test_session_path_lands_in_sessions_dir(platform_root):
    assert registry.session_path("s-1") == platform_root / "sessions" / "s-1.json"


def test_token_path_is_the_entry_s_sibling(platform_root):
    assert registry.token_path("s-1") == platform_root / "sessions" / "s-1.token"


@pytest.mark.parametrize("bad", ["", "../escape", "a/b", None])
def test_token_path_rejects_unsafe_ids(platform_root, bad):
    """Same validation as the entry: a credential path must not accept ``..``."""
    with pytest.raises(registry.RegistryError):
        registry.token_path(bad)


# --- register ---------------------------------------------------------------

def test_register_roundtrip(platform_root, live_pid):
    entry = registry.register(
        "s-1",
        pid=live_pid,
        task={"taskdef": "develop", "target": "issue-141"},
        run={"slug": "develop-issue-141"},
        control={"host": "127.0.0.1", "port": 8731, "token_ref": "/run/tok"},
        ports=[{"host": 30021, "container": 3000}],
        container_id="9c1f",
        log_path="/logs/s-1.log",
    )
    assert entry["kind"] == "worker"
    assert entry["pid"] == live_pid
    assert entry["owner_pid"] == os.getpid()

    stored = registry.read_session("s-1")
    assert stored["task"]["taskdef"] == "develop"
    assert stored["ports"] == [{"host": 30021, "container": 3000}]
    assert stored["container_id"] == "9c1f"
    assert stored["started_at"].endswith("Z")


def test_register_defaults_optional_blocks(platform_root, live_pid):
    entry = registry.register("s-1", pid=live_pid)
    assert entry["task"] == {}
    assert entry["ports"] == []
    assert entry["slot"] is None


def test_register_rejects_unknown_kind(platform_root, live_pid):
    with pytest.raises(registry.RegistryError, match="invalid session kind"):
        registry.register("s-1", kind="supervisor", pid=live_pid)


def test_register_accepts_assistant_kind(platform_root, live_pid):
    assert registry.register("s-1", kind="assistant", pid=live_pid)["kind"] == "assistant"


@pytest.mark.parametrize("bad_pid", [0, -1, "123", None, True])
def test_register_rejects_bad_pid(platform_root, bad_pid):
    with pytest.raises(registry.RegistryError, match="invalid pid"):
        registry.register("s-1", pid=bad_pid)


def test_register_refuses_inline_token(platform_root, live_pid):
    """Entries get cat'd and pasted; a full-control token must not be in one."""
    with pytest.raises(registry.RegistryError, match="token_ref"):
        registry.register(
            "s-1", pid=live_pid, control={"port": 8731, "token": "super-secret"}
        )


def test_registered_file_contains_no_token(platform_root, live_pid):
    registry.register(
        "s-1", pid=live_pid, control={"port": 8731, "token_ref": "/run/tok"}
    )
    raw = registry.session_path("s-1").read_text(encoding="utf-8")
    assert "token_ref" in raw
    assert '"token"' not in raw


# --- liveness ---------------------------------------------------------------

def test_is_live_true_for_this_process(live_pid):
    assert registry.is_live({"pid": live_pid}) is True


@pytest.mark.parametrize("entry", [None, {}, {"pid": 0}, {"pid": -5}, {"pid": "x"},
                                   {"pid": True}, {"pid": DEAD_PID}])
def test_is_live_false_for_nonexistent_or_malformed(entry):
    assert registry.is_live(entry) is False


def test_permission_error_counts_as_alive(monkeypatch):
    """Another user's process exists; better shown than silently dropped."""
    def denied(_pid, _sig):
        raise PermissionError

    monkeypatch.setattr(os, "kill", denied)
    assert registry.is_live({"pid": 4242}) is True


def test_liveness_reads_session_pid_not_owner_pid(platform_root):
    """Recording the daemon's PID would make every session look alive."""
    registry.register("s-dead", pid=DEAD_PID)
    entry = registry.read_session("s-dead")
    assert entry["owner_pid"] == os.getpid()   # the writer is alive
    assert registry.is_live(entry) is False    # the session is not


# --- listing ----------------------------------------------------------------

def test_list_sessions_filters_stale_by_default(platform_root, live_pid):
    registry.register("s-live", pid=live_pid)
    registry.register("s-dead", pid=DEAD_PID)

    ids = [e["id"] for e in registry.list_sessions()]
    assert ids == ["s-live"]

    all_ids = sorted(e["id"] for e in registry.list_sessions(live_only=False))
    assert all_ids == ["s-dead", "s-live"]


def test_list_sessions_annotates_live_without_persisting_it(platform_root, live_pid):
    registry.register("s-1", pid=live_pid)
    assert registry.list_sessions()[0]["live"] is True
    on_disk = json.loads(registry.session_path("s-1").read_text(encoding="utf-8"))
    assert "live" not in on_disk


def test_list_sessions_does_not_mutate_stale_entries(platform_root):
    """Reads are side-effect free — unlinking by path races with a register."""
    registry.register("s-dead", pid=DEAD_PID)
    registry.list_sessions()
    assert registry.session_path("s-dead").exists()


def test_list_sessions_orders_by_start_time(platform_root, live_pid):
    registry.register("s-b", pid=live_pid, started_at="2026-07-26T10:00:00Z")
    registry.register("s-a", pid=live_pid, started_at="2026-07-26T09:00:00Z")
    assert [e["id"] for e in registry.list_sessions()] == ["s-a", "s-b"]


def test_list_sessions_survives_one_corrupt_entry(platform_root, live_pid):
    """One bad file must not take out the whole fleet view."""
    registry.register("s-good", pid=live_pid)
    store.sessions_dir().joinpath("s-bad.json").write_text("{not json", encoding="utf-8")

    ids = [e["id"] for e in registry.list_sessions()]
    assert ids == ["s-good"]


def test_list_sessions_empty_when_no_directory(platform_root):
    assert registry.list_sessions() == []


# --- update -----------------------------------------------------------------

def test_update_merges_late_arriving_facts(platform_root, live_pid):
    registry.register("s-1", pid=live_pid)
    updated = registry.update(
        "s-1", container_id="abc123", ports=[{"host": 30022, "container": 8000}]
    )
    assert updated["container_id"] == "abc123"
    assert registry.read_session("s-1")["ports"][0]["host"] == 30022
    assert registry.read_session("s-1")["pid"] == live_pid


def test_update_absent_session_returns_none(platform_root):
    assert registry.update("s-missing", container_id="x") is None


def test_update_refuses_inline_token(platform_root, live_pid):
    registry.register("s-1", pid=live_pid)
    with pytest.raises(registry.RegistryError, match="token_ref"):
        registry.update("s-1", control={"token": "nope"})


def test_update_refreshes_owner_pid(platform_root, live_pid):
    registry.register("s-1", pid=live_pid)
    path = registry.session_path("s-1")
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["owner_pid"] = DEAD_PID
    path.write_text(json.dumps(entry), encoding="utf-8")

    registry.update("s-1", container_id="x")
    assert registry.read_session("s-1")["owner_pid"] == os.getpid()


# --- remove -----------------------------------------------------------------

def test_remove_deletes_own_entry(platform_root, live_pid):
    registry.register("s-1", pid=live_pid)
    assert registry.remove("s-1") is True
    assert registry.read_session("s-1") is None


def test_remove_absent_entry_is_false(platform_root):
    assert registry.remove("s-1") is False


def test_remove_skips_entry_owned_by_another_live_writer(platform_root, live_pid,
                                                        monkeypatch):
    """Two daemons must not delete each other's entries."""
    registry.register("s-1", pid=live_pid)
    path = registry.session_path("s-1")
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["owner_pid"] = live_pid + 1
    path.write_text(json.dumps(entry), encoding="utf-8")

    monkeypatch.setattr(registry, "_pid_alive", lambda pid: True)
    assert registry.remove("s-1") is False
    assert path.exists()


def test_remove_reclaims_entry_whose_owner_is_dead(platform_root, live_pid):
    """After a daemon crash the successor must be able to clean up."""
    registry.register("s-1", pid=live_pid)
    path = registry.session_path("s-1")
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["owner_pid"] = DEAD_PID
    path.write_text(json.dumps(entry), encoding="utf-8")

    assert registry.remove("s-1") is True


def test_remove_force_overrides_ownership(platform_root, live_pid, monkeypatch):
    registry.register("s-1", pid=live_pid)
    path = registry.session_path("s-1")
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["owner_pid"] = live_pid + 1
    path.write_text(json.dumps(entry), encoding="utf-8")
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: True)

    assert registry.remove("s-1", force=True) is True


def _write_token(session_id: str, text: str = "tok") -> "os.PathLike":
    path = registry.token_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_remove_takes_the_control_token_with_the_entry(platform_root, live_pid):
    """Otherwise every removed session leaves a live credential in sessions/."""
    registry.register(
        "s-1", pid=live_pid,
        control={"host": "127.0.0.1", "port": 8731,
                 "token_ref": str(registry.token_path("s-1"))},
    )
    token = _write_token("s-1")

    assert registry.remove("s-1") is True
    assert not token.exists()


def test_remove_without_a_token_still_succeeds(platform_root, live_pid):
    """Not every session has one — an older entry, or a spawn that never got that far."""
    registry.register("s-1", pid=live_pid)
    assert registry.remove("s-1") is True


def test_a_refused_removal_leaves_the_token_alone(platform_root, live_pid, monkeypatch):
    """The token goes only when the entry went; here the entry stays put."""
    registry.register("s-1", pid=live_pid)
    path = registry.session_path("s-1")
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["owner_pid"] = live_pid + 1
    path.write_text(json.dumps(entry), encoding="utf-8")
    token = _write_token("s-1")
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: True)

    assert registry.remove("s-1") is False
    assert token.exists(), "the owning daemon's session is still live and drivable"


def test_remove_never_follows_the_recorded_token_ref(platform_root, live_pid, tmp_path):
    """``token_ref`` is caller data; unlinking it would be an arbitrary-file delete."""
    elsewhere = tmp_path / "not-ours.txt"
    elsewhere.write_text("innocent", encoding="utf-8")
    registry.register(
        "s-1", pid=live_pid,
        control={"port": 8731, "token_ref": str(elsewhere)},
    )

    assert registry.remove("s-1") is True
    assert elsewhere.exists()


def test_corrupt_entry_self_heals_on_first_read(platform_root):
    """The reader relocates a corrupt entry, so the registry drops it by itself.

    ``store.read_json`` moves unparseable bytes to ``.bad-<stamp>`` before
    raising, which means a corrupt entry leaves the registry on the first read
    that touches it — no explicit removal needed, and the evidence survives.
    ``remove`` then truthfully reports that it unlinked nothing.
    """
    sessions = store.sessions_dir()
    sessions.mkdir(parents=True, exist_ok=True)
    sessions.joinpath("s-bad.json").write_text("{not json", encoding="utf-8")

    assert registry.read_session("s-bad") is None
    assert not sessions.joinpath("s-bad.json").exists()
    assert len(list(sessions.glob("s-bad.json.bad-*"))) == 1
    assert registry.remove("s-bad") is False


# --- prune ------------------------------------------------------------------

def test_prune_dead_removes_only_dead_sessions(platform_root, live_pid):
    registry.register("s-live", pid=live_pid)
    registry.register("s-dead", pid=DEAD_PID)

    assert registry.prune_dead() == ["s-dead"]
    assert [e["id"] for e in registry.list_sessions(live_only=False)] == ["s-live"]


def test_prune_dead_is_idempotent(platform_root, live_pid):
    registry.register("s-dead", pid=DEAD_PID)
    registry.prune_dead()
    assert registry.prune_dead() == []


def test_prune_dead_reclaims_entries_from_a_previous_daemon(platform_root):
    """Unique ids mean nothing overwrites a dead entry — this is the reclaim."""
    registry.register("s-dead", pid=DEAD_PID)
    path = registry.session_path("s-dead")
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["owner_pid"] = DEAD_PID + 1
    path.write_text(json.dumps(entry), encoding="utf-8")

    assert registry.prune_dead() == ["s-dead"]


def test_prune_dead_no_sessions_is_empty(platform_root):
    assert registry.prune_dead() == []


def test_prune_dead_leaves_no_orphan_tokens(platform_root, live_pid):
    """The litter case: pruning is what deletes the entry that explained the token."""
    registry.register("s-dead", pid=DEAD_PID)
    registry.register("s-live", pid=live_pid)
    dead_token = _write_token("s-dead")
    live_token = _write_token("s-live")

    assert registry.prune_dead() == ["s-dead"]
    assert not dead_token.exists()
    assert live_token.exists(), "a live session's token must survive the sweep"


# --- zombies ----------------------------------------------------------------
#
# The daemon is the parent of every session it spawns, so an exited-but-unreaped
# child still answers kill(pid, 0). Counting that as alive made a `kill -9`'d
# session keep reporting as running.

def test_zombie_process_is_not_live():
    import subprocess

    child = subprocess.Popen(["/bin/true"])
    try:
        # Do not wait(): the child becomes a zombie that kill(pid, 0) still finds.
        deadline = __import__("time").monotonic() + 5.0
        while __import__("time").monotonic() < deadline:
            if registry._is_zombie(child.pid):
                break
            __import__("time").sleep(0.02)

        assert registry._is_zombie(child.pid) is True, "expected an unreaped zombie"
        assert registry._pid_alive(child.pid) is False
        assert registry.is_live({"pid": child.pid}) is False
    finally:
        child.wait()


def test_live_process_is_not_a_zombie():
    assert registry._is_zombie(os.getpid()) is False
    assert registry._pid_alive(os.getpid()) is True


def test_zombie_check_degrades_when_proc_is_unavailable(monkeypatch):
    """No /proc (macOS): fall back to the kill() answer rather than erroring."""
    def no_proc(*_args, **_kwargs):
        raise FileNotFoundError("/proc")

    monkeypatch.setattr("builtins.open", no_proc)
    assert registry._is_zombie(os.getpid()) is False


def test_zombie_check_tolerates_a_comm_containing_parens(monkeypatch, tmp_path):
    """`comm` is parenthesised and may contain spaces or parens of its own."""
    fake = tmp_path / "stat"
    fake.write_text("1234 (weird (name) here) Z 1 1 1\n", encoding="utf-8")
    real_open = open

    def fake_open(path, *args, **kwargs):
        if str(path).startswith("/proc/"):
            return real_open(fake, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    assert registry._is_zombie(4242) is True
