"""Tests for the platform state store (issue #141, slice M1 / T1).

Covers the guarantees the store exists to provide: writes land whole or not at
all, every snapshot is published owner-only — with no window in which it is not —
reads never explode and never destroy the evidence, and history survives a crash
mid-append.

The mode group has a second half (T93): the tree *around* the snapshots is
owner-only too, at every level and on a directory an earlier build already left
wide, because names and mtimes are the shape of the fleet.
"""

import json
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from lmer_platform import store
from tests.conftest import denied_create, strip_lmer_env


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    """Point the store at a temp dir so nothing touches the real state dir."""
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


# --- directory resolution ---------------------------------------------------

def test_platform_dir_honors_override(platform_root):
    assert store.platform_dir() == platform_root
    assert store.sessions_dir() == platform_root / "sessions"
    assert store.logs_dir() == platform_root / "logs"
    assert store.events_path() == platform_root / "events.jsonl"
    assert store.snapshot_path("queue.json") == platform_root / "queue.json"


def test_platform_dir_defaults_under_state_dir(monkeypatch):
    """Without an override the tree lives beside the other lmer state."""
    monkeypatch.setattr(store, "PLATFORM_DIR", None)
    monkeypatch.setattr(store, "lmer_state_dir", lambda: Path("/fake/state"))
    assert store.platform_dir() == Path("/fake/state/platform")


# --- write_json -------------------------------------------------------------

def test_write_json_roundtrip_stamps_schema_and_updated(platform_root):
    path = store.snapshot_path("queue.json")
    store.write_json(path, {"items": [1, 2, 3]})

    data = store.read_json(path)
    assert data["items"] == [1, 2, 3]
    assert data["schema"] == store.SCHEMA_VERSION
    assert data["updated"].endswith("Z")


def test_write_json_creates_parent_dirs(platform_root):
    path = store.sessions_dir() / "s-1.json"
    store.write_json(path, {"id": "s-1"})
    assert path.is_file()


def test_write_json_does_not_mutate_caller_payload(platform_root):
    payload = {"items": []}
    store.write_json(store.snapshot_path("queue.json"), payload)
    assert payload == {"items": []}


def test_write_json_leaves_no_temp_file_behind(platform_root):
    store.write_json(store.snapshot_path("queue.json"), {"items": []})
    leftovers = [p.name for p in platform_root.iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_write_json_is_human_readable(platform_root):
    """D2's whole point is a file you can repair in an editor."""
    path = store.snapshot_path("config.json")
    store.write_json(path, {"bind_port": 8730})
    text = path.read_text(encoding="utf-8")
    assert "\n" in text.strip()
    assert text.endswith("\n")


def test_write_json_replaces_previous_snapshot(platform_root):
    path = store.snapshot_path("queue.json")
    store.write_json(path, {"items": ["old"]})
    store.write_json(path, {"items": ["new"]})
    assert store.read_json(path)["items"] == ["new"]


def test_write_json_rejects_non_mapping(platform_root):
    with pytest.raises(store.StoreError, match="must be a mapping"):
        store.write_json(store.snapshot_path("queue.json"), ["not", "a", "mapping"])


def test_write_json_raises_when_target_unwritable(platform_root, monkeypatch):
    """A failed write is loud: the queue is the one thing not reconstructible."""
    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    with pytest.raises(store.StoreError, match="cannot write"):
        store.write_json(store.snapshot_path("queue.json"), {"items": []})


def test_write_json_cleans_up_temp_on_failure(platform_root, monkeypatch):
    path = store.snapshot_path("queue.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    real_replace = Path.replace

    def fail_replace(self, target):
        raise OSError("cross-device link")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(store.StoreError):
        store.write_json(path, {"items": []})
    monkeypatch.setattr(Path, "replace", real_replace)

    assert list(platform_root.glob(".*.tmp")) == []


def test_write_json_temp_name_carries_pid(platform_root, monkeypatch):
    """Two processes writing the same target must not share a temp path."""
    seen = {}
    real_write_text = Path.write_text

    def capture(self, *args, **kwargs):
        seen["tmp"] = self.name
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", capture)
    store.write_json(store.snapshot_path("queue.json"), {"items": []})
    assert str(os.getpid()) in seen["tmp"]


def test_write_json_temp_file_lives_beside_its_target(platform_root, monkeypatch):
    """A temp anywhere else would make the rename a copy, and a copy is not atomic."""
    seen = {}
    real_write_text = Path.write_text

    def capture(self, *args, **kwargs):
        seen["tmp"] = self
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", capture)
    path = store.sessions_dir() / "s-1.json"
    store.write_json(path, {"id": "s-1"})

    assert seen["tmp"].parent == path.parent
    assert seen["tmp"].name.startswith("."), "a visible temp reads as state"
    assert seen["tmp"].name.endswith(".tmp")


# --- the mode a snapshot is published with -----------------------------------
#
# One rule for every snapshot, not one per file: what they hold ranges from status
# to operator and agent prose, and ``assistant.json`` holds three pieces of
# agent-authored text at once — a handoff, a digest spool and the standing orders,
# any of which can quote a credential. Nothing out of this process reads any of
# them (the UI reads through the daemon), so the wider bits buy nothing.

@pytest.mark.parametrize("name", ["queue.json", "assistant.json", "config.json"])
def test_write_json_publishes_an_owner_only_snapshot(platform_root, name):
    store.write_json(store.snapshot_path(name), {"items": []})
    mode = stat.S_IMODE(store.snapshot_path(name).stat().st_mode)
    assert mode == 0o600, f"{name} is mode {mode:o}"


def test_a_session_registry_entry_is_owner_only_too(platform_root):
    """The blanket half: a snapshot in a subdirectory is not a special case."""
    path = store.sessions_dir() / "s-1.json"
    store.write_json(path, {"id": "s-1"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_temp_is_owner_only_before_it_holds_anything(platform_root, monkeypatch):
    """The window is the point, so the mode cannot be a ``chmod`` after the write.

    The writer is held at the boundary between creating its temp and writing the
    payload into it — where a mode corrected afterwards has not been corrected yet
    — and the mode is read off the real file at that moment rather than inferred
    from the order of calls. An empty temp is what proves the boundary was the one
    caught: with the mode set after the bytes, this reads 0644 with a payload in
    it.

    The umask is pinned for the length of the check, as in the assistant's env
    file: it can only *remove* bits, so a strict one on the host would mask a wide
    create from this assertion.
    """
    path = store.snapshot_path("assistant.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    real_write_text = Path.write_text
    at_the_boundary = threading.Event()
    may_write = threading.Event()
    seen, errors = {}, []

    def hold_before_writing(self, *args, **kwargs):
        seen["tmp"] = self
        at_the_boundary.set()
        may_write.wait(timeout=60)
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", hold_before_writing)

    def write():
        try:
            store.write_json(path, {"handoff": "the fleet is quiet"})
        except BaseException as exc:  # pragma: no cover - a failure prints the reason
            errors.append(exc)

    previous = os.umask(0o022)
    writer = threading.Thread(target=write, name="held-writer")
    writer.start()
    try:
        assert at_the_boundary.wait(timeout=30), "the writer never reached its temp"
        observed = seen["tmp"].stat()
    finally:
        may_write.set()
        writer.join(timeout=30)
        os.umask(previous)

    assert not errors, errors
    assert not writer.is_alive(), "the writer never finished"
    assert observed.st_size == 0, "the temp already held the payload"
    mode = stat.S_IMODE(observed.st_mode)
    assert mode == 0o600, (
        f"the temp was mode {mode:o} while it was being written"
    )


def test_a_leftover_temp_with_looser_bits_is_tightened(platform_root):
    """``os.open`` ignores its mode for a file that already exists.

    A temp left behind by a crashed write is reused by the writer that shares its
    name — same process, same thread — so its mode has to be corrected rather than
    inherited, and corrected while the file is still empty. The name is spelled out
    here because it is exactly the one this writer will pick.
    """
    path = store.snapshot_path("assistant.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    stale = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    stale.write_text("{}\n", encoding="utf-8")
    stale.chmod(0o644)

    store.write_json(path, {"handoff": "the fleet is quiet"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_world_readable_snapshot_is_republished_owner_only(platform_root):
    """A file written by a build that had no mode rule is tightened by the rewrite,
    because the rename publishes the temp's inode rather than reusing the target's."""
    path = store.snapshot_path("assistant.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    path.chmod(0o644)

    store.write_json(path, {"handoff": "the fleet is quiet"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# --- the mode the tree around the snapshots has ------------------------------
#
# Owner-only contents in a traversable directory still publish the metadata: which
# sessions exist, when each was last written, that a ``.bad-`` backup happened at
# 03:12. Every check below pins the umask, which can only *remove* bits — a strict
# one on the host would hide a wide create from these assertions.

def test_a_snapshot_lands_in_an_owner_only_directory(platform_root):
    previous = os.umask(0o022)
    try:
        store.write_json(store.snapshot_path("queue.json"), {"items": []})
    finally:
        os.umask(previous)
    mode = stat.S_IMODE(platform_root.stat().st_mode)
    assert mode == 0o700, f"the platform root is mode {mode:o}"


def test_every_level_of_a_nested_state_dir_is_owner_only(platform_root):
    """``mkdir(parents=True)`` applies its mode to the *leaf* only.

    So a registry entry's directory is the case that catches a mode passed to
    pathlib and left there: ``sessions/`` comes out 0700 and the ``platform/`` it
    was created inside comes out 0755, which is the directory that holds every
    other snapshot.
    """
    previous = os.umask(0o022)
    try:
        store.write_json(store.sessions_dir() / "s-1.json", {"id": "s-1"})
    finally:
        os.umask(previous)
    modes = {
        level.name: stat.S_IMODE(level.stat().st_mode)
        for level in (platform_root, store.sessions_dir())
    }
    assert modes == {"platform": 0o700, "sessions": 0o700}


def test_the_history_directory_is_owner_only_too(platform_root):
    """``append_event`` creates the tree on its own on a host that has only ever
    logged — and it is the same tree."""
    previous = os.umask(0o022)
    try:
        store.append_event("session_spawned", note="s-1")
    finally:
        os.umask(previous)
    assert stat.S_IMODE(platform_root.stat().st_mode) == 0o700


def test_a_pre_existing_state_dir_is_tightened_on_write(platform_root):
    """The upgrade case, and the whole reason the chmod is not skipped.

    ``mkdir`` does nothing at all to a directory that exists, so a tree an earlier
    build created 0755 would keep those bits for as long as the host lives — the
    fix would reach only the hosts that never needed it.
    """
    nested = store.sessions_dir()
    nested.mkdir(parents=True)
    platform_root.chmod(0o755)
    nested.chmod(0o750)

    store.write_json(nested / "s-1.json", {"id": "s-1"})
    assert stat.S_IMODE(platform_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(nested.stat().st_mode) == 0o700


def test_a_narrower_state_dir_is_left_as_the_operator_set_it(platform_root):
    """Tightening is not "set the mode": only bits outside 0700 trigger a chmod,
    so a tree somebody deliberately made narrower is not widened back.

    The mode is real and the refusal is injected — root is exempt from the
    permission check, so the kernel would let the write through and the mode
    assertion would be the only half of this that ran. See ``denied_create``.
    """
    platform_root.mkdir(parents=True)
    platform_root.chmod(0o500)
    try:
        with denied_create(platform_root):
            with pytest.raises(store.StoreError):
                # Unwritable is the operator's business; the mode is what is
                # asserted.
                store.write_json(store.snapshot_path("queue.json"), {"items": []})
        assert stat.S_IMODE(platform_root.stat().st_mode) == 0o500
    finally:
        platform_root.chmod(0o700)


def test_the_tightened_levels_stop_at_the_platform_root(platform_root):
    """Ownership stops at the platform root, and is asserted on the walk itself.

    Above the root is the lmer state dir, which holds directories a spawn mounts
    into a container and caches this module knows nothing about — creating it is
    fine, choosing its mode is not this module's to do. A directory *outside* the
    tree is one level for the same reason: whoever chose where it lives keeps its
    parents.
    """
    assert store._owned_levels(store.sessions_dir()) == [
        platform_root, store.sessions_dir()
    ]
    assert store._owned_levels(platform_root) == [platform_root]
    outside = platform_root.parent / "elsewhere" / "deep"
    assert store._owned_levels(outside) == [outside]


def test_the_state_dir_above_the_platform_root_is_not_touched(platform_root):
    """The same boundary, observed as a mode rather than as a list of levels."""
    above = platform_root.parent
    above.chmod(0o755)
    try:
        store.write_json(store.sessions_dir() / "s-1.json", {"id": "s-1"})
        assert stat.S_IMODE(above.stat().st_mode) == 0o755
    finally:
        above.chmod(0o700)


def test_the_directory_is_owner_only_before_the_temp_appears(
    platform_root, monkeypatch
):
    """The directory is what guards the temp *name*, so it cannot be tightened after.

    There is no ``O_EXCL`` on the temp and it is reopened by name for the write
    (see :func:`store.write_json`), so what stops a planted symlink under that name
    is that nobody else can create one — which is only true if the mode is on the
    directory before the temp appears in it, not chmod'ed on once the snapshot is
    published. Read off the real directory at that moment rather than inferred from
    the order of calls.
    """
    real_create = store._create_owner_only
    seen = {}

    def record_the_directory(tmp):
        seen["mode"] = stat.S_IMODE(tmp.parent.stat().st_mode)
        return real_create(tmp)

    monkeypatch.setattr(store, "_create_owner_only", record_the_directory)
    previous = os.umask(0o022)
    try:
        store.write_json(store.snapshot_path("assistant.json"), {"items": []})
    finally:
        os.umask(previous)

    assert seen["mode"] == 0o700, (
        f"the temp was created in a mode {seen['mode']:o} directory"
    )


# --- concurrent writers -----------------------------------------------------

def test_two_threads_writing_one_snapshot_do_not_overlap(platform_root, monkeypatch):
    """Sync API handlers run in a threadpool, so one process is many writers.

    Two writers inside one file's write at the same moment is the interleaving
    every other guarantee in this module is downstream of: with a shared temp
    path the winner renames the temp away while the loser, still holding an fd on
    that inode, writes its payload over the snapshot just published and then
    fails ENOENT renaming a file that is no longer there. The temp name carries
    the writer's thread id so that cannot happen even without a lock — but the
    lock is what makes the *sequence* one operation, and this asserts it holds
    for the write on its own, not only inside a :func:`store.mutating` block.

    The sleep inside ``replace`` is what makes the assertion mean something: it
    is the window a real race needs, held open on purpose rather than left to a
    one-CPU scheduler to produce.
    """
    path = store.snapshot_path("queue.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    real_replace = Path.replace
    guard = threading.Lock()
    inside = 0
    overlaps = []

    def replace_recording_overlap(self, target):
        nonlocal inside
        with guard:
            inside += 1
            if inside > 1:
                overlaps.append(inside)
        try:
            time.sleep(0.05)
            return real_replace(self, target)
        finally:
            with guard:
                inside -= 1

    monkeypatch.setattr(Path, "replace", replace_recording_overlap)
    errors = []

    def write(index):
        try:
            store.write_json(path, {"writer": index})
        except Exception as exc:  # pragma: no cover - a failure prints the reason
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not [t for t in threads if t.is_alive()], "a writer never finished"
    assert not errors, errors
    assert overlaps == [], "two writers were inside one snapshot's write at once"
    assert store.read_json(path)["writer"] in (0, 1)
    assert list(platform_root.glob(".*.tmp")) == [], "a temp outlived the race"


def test_a_read_modify_write_is_not_interleaved_with_another(platform_root):
    """The gap per-write atomicity never closed, and what :func:`store.mutating`
    is for: two threads each read the whole snapshot, change one key and write it
    back. Unserialised, the loser's key is simply gone — which in ``runs.json``
    is a spawned run that never appears in the fleet view while its container
    runs, and that ``resume`` and ``answer`` then refuse as ``run_not_tracked``.
    """
    path = store.snapshot_path("runs.json")
    store.write_json(path, {"runs": {}})
    ready = threading.Barrier(4, timeout=10)
    errors = []

    def add(index):
        try:
            ready.wait()
            with store.mutating(path):
                runs = store.read_json(path)["runs"]
                time.sleep(0.02)  # the window between the read and the write
                runs[f"host/project/run-{index}"] = {"seen": index}
                store.write_json(path, {"runs": runs})
        except Exception as exc:  # pragma: no cover - a failure prints the reason
            errors.append(exc)

    threads = [threading.Thread(target=add, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert sorted(store.read_json(path)["runs"]) == [
        f"host/project/run-{index}" for index in range(4)
    ], "a concurrent writer's key was dropped"


def test_a_concurrent_reader_never_sees_a_half_written_snapshot(platform_root):
    """The property temp-plus-rename exists to provide, under a real race.

    Four threads rewrite one snapshot while a reader hammers it. Every read must
    be a whole record — the previous snapshot or a newer one — and never an
    empty file, a truncated one, or two payloads run together.
    """
    path = store.snapshot_path("queue.json")
    store.write_json(path, {"writer": -1, "seq": -1})

    writers, rounds = 4, 25
    start = threading.Barrier(writers + 1, timeout=10)
    stop = threading.Event()
    errors, torn, observed = [], [], []
    reads = 0

    def write(index):
        try:
            start.wait()
            for seq in range(rounds):
                store.write_json(path, {"writer": index, "seq": seq})
        except Exception as exc:  # pragma: no cover - a failure prints the reason
            errors.append(exc)

    def read():
        nonlocal reads
        try:
            start.wait()
            while not stop.is_set():
                reads += 1
                try:
                    raw = path.read_text(encoding="utf-8")
                except FileNotFoundError:
                    torn.append("<the snapshot vanished>")
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    torn.append(raw)
                    continue
                if (record.get("schema") != store.SCHEMA_VERSION
                        or "updated" not in record):
                    torn.append(raw)
                    continue
                mark = (record.get("writer"), record.get("seq"))
                if not observed or observed[-1] != mark:
                    observed.append(mark)
        except Exception as exc:  # pragma: no cover - a failure prints the reason
            errors.append(exc)

    threads = [
        threading.Thread(target=write, args=(index,)) for index in range(writers)
    ]
    reader = threading.Thread(target=read)
    reader.start()
    for thread in threads:
        thread.start()
    try:
        for thread in threads:
            thread.join(timeout=60)
    finally:
        stop.set()
        reader.join(timeout=30)

    assert not errors, errors
    assert not torn, f"the reader saw {len(torn)} half-written snapshot(s): {torn[:1]}"
    assert reads, "the reader never got a look in"
    assert len(observed) > 1, f"the reader never caught a rewrite ({reads} reads)"
    assert all(-1 <= writer < writers for writer, _ in observed), observed
    assert all(-1 <= seq < rounds for _, seq in observed), observed

    final = store.read_json(path)
    assert 0 <= final["writer"] < writers and 0 <= final["seq"] < rounds
    assert list(platform_root.glob(".*.tmp")) == [], "a temp outlived the race"


# --- read_json --------------------------------------------------------------

def test_read_json_missing_file_is_none(platform_root):
    assert store.read_json(store.snapshot_path("absent.json")) is None


def test_read_json_without_schema_reads_as_version_zero(platform_root):
    """Hand-seeded config is legitimate; the next write stamps the version."""
    path = store.snapshot_path("config.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"bind_port": 9000}), encoding="utf-8")
    assert store.read_json(path)["bind_port"] == 9000


@pytest.mark.parametrize(
    "content,reason",
    [
        ("{not json", "unparseable"),
        ("[1, 2, 3]", "not a JSON object"),
        ('{"schema": "one"}', "not an integer"),
        ('{"schema": true}', "not an integer"),
    ],
)
def test_read_json_backs_up_corrupt_file(platform_root, content, reason):
    path = store.snapshot_path("queue.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    with pytest.raises(store.StoreError, match=reason):
        store.read_json(path)

    assert not path.exists(), "corrupt file should be moved aside, not left in place"
    backups = list(platform_root.glob("queue.json.bad-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == content, "evidence must survive"


def test_read_json_newer_schema_refuses_without_touching_file(platform_root):
    """A future version wrote this; destroying it would break that version."""
    path = store.snapshot_path("queue.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps({"schema": store.SCHEMA_VERSION + 1, "items": []})
    path.write_text(original, encoding="utf-8")

    with pytest.raises(store.StoreError, match="newer than supported"):
        store.read_json(path)

    assert path.read_text(encoding="utf-8") == original
    assert list(platform_root.glob("queue.json.bad-*")) == []


def test_read_json_reports_when_backup_also_fails(platform_root, monkeypatch):
    path = store.snapshot_path("queue.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    def no_rename(self, target):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "rename", no_rename)
    with pytest.raises(store.StoreError, match="could not be moved aside"):
        store.read_json(path)


def test_read_json_supported_version_is_overridable(platform_root):
    path = store.snapshot_path("queue.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": 5, "items": []}), encoding="utf-8")
    assert store.read_json(path, supported_version=5)["items"] == []


# --- events -----------------------------------------------------------------

def test_append_event_writes_one_line_per_event(platform_root):
    store.append_event("session_spawned", note="s-1")
    store.append_event("session_exited", data={"session": "s-1", "code": 0})

    lines = store.events_path().read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert first["type"] == "session_spawned"
    assert first["note"] == "s-1"
    assert "data" not in first
    assert second["data"] == {"session": "s-1", "code": 0}
    assert first["ts"].endswith("Z")


def test_the_history_file_is_owner_only_on_create(platform_root):
    """The history carries the same agent-quoted text the snapshots do
    (assistant digests among it) — it must not be the one file in an
    owner-only tree that takes the umask."""
    store.append_event("session_spawned", note="s-1")

    mode = store.events_path().stat().st_mode & 0o777
    assert mode == 0o600


def test_read_events_returns_all_by_default(platform_root):
    for i in range(4):
        store.append_event("tick", note=str(i))
    events = store.read_events()
    assert [e["note"] for e in events] == ["0", "1", "2", "3"]


def test_read_events_last_n_returns_the_newest(platform_root):
    for i in range(5):
        store.append_event("tick", note=str(i))
    assert [e["note"] for e in store.read_events(2)] == ["3", "4"]


def test_read_events_missing_file_is_empty(platform_root):
    assert store.read_events() == []


def test_read_events_tolerates_torn_final_line(platform_root):
    """A crash mid-append must not make the whole log unreadable."""
    store.append_event("tick", note="good")
    with open(store.events_path(), "a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-07-26T00:00:00Z", "type": "tor')

    events = store.read_events()
    assert [e["note"] for e in events] == ["good"]


def test_read_events_skips_blank_and_non_object_lines(platform_root):
    store.append_event("tick", note="good")
    with open(store.events_path(), "a", encoding="utf-8") as fh:
        fh.write("\n\n")
        fh.write("[1, 2, 3]\n")
        fh.write('"a string"\n')

    assert [e["note"] for e in store.read_events()] == ["good"]


def test_append_event_survives_unwritable_log(platform_root, monkeypatch):
    """History is best-effort: it must not break the operation it annotates."""
    def boom(*_args, **_kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr("builtins.open", boom)
    store.append_event("tick")  # must not raise


def test_append_event_honors_explicit_path(platform_root, tmp_path):
    other = tmp_path / "other" / "history.jsonl"
    store.append_event("tick", path=other)
    assert store.read_events(path=other)[0]["type"] == "tick"
    assert not store.events_path().exists()
