"""The assistant's platform-local memory store (issue #325).

Four properties, each of which fails quietly: the store is not in the work repo,
it survives an incarnation, it is observed but never trimmed, and a measurement
cannot be talked into following a symlink out of it.
"""

import logging
import os
import stat
from pathlib import Path

import pytest

from lmer_cli.harness import HARNESSES
from lmer_cli.mounts import CONTAINER_MOUNT_STAGING_DIR
from lmer_platform import memory, store
from lmer_platform import config as cfg
from tests.conftest import strip_lmer_env


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in ("LMER_WORK_REPO", cfg.ENV_BIND_ADDRESS, cfg.ENV_BIND_PORT):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


def test_the_store_lives_under_the_platform_state_tree(platform_root):
    """The work repo syncs to every other host; the platform state tree does not."""
    directory = memory.memory_dir()
    assert directory.parent == store.platform_dir()
    assert directory.parent == platform_root


def test_the_store_is_private_to_this_user(platform_root):
    """Agent-written notes, rw-mounted into a container — the transcript's mode."""
    directory = memory.prepare_memory_dir()
    assert directory is not None
    mode = stat.S_IMODE(directory.stat().st_mode)
    assert mode == 0o700, f"expected 0700, got {oct(mode)}"


def test_what_one_incarnation_wrote_is_there_for_the_next(platform_root):
    """The issue in one assertion: preparing the store again finds the file."""
    first = memory.prepare_memory_dir()
    (first / "MEMORY.md").write_text("- [work goes stale](stale.md)\n", encoding="utf-8")
    second = memory.prepare_memory_dir()
    assert second == first
    assert (second / "MEMORY.md").read_text(encoding="utf-8").startswith("- [work")


def test_an_unusable_store_is_reported_and_not_fatal(platform_root, monkeypatch, caplog):
    """Fail-soft: the assistant still starts, it just forgets."""
    def refuse(_directory):
        raise OSError("read-only file system")

    monkeypatch.setattr(store, "ensure_state_dir", refuse)
    with caplog.at_level(logging.WARNING, logger="lmer_platform.memory"):
        assert memory.prepare_memory_dir() is None
    assert any(
        "platform_assistant_memory_dir_unusable" in record.getMessage()
        for record in caplog.records
    )


def test_every_declaring_harness_is_linked_to_the_one_staged_mount():
    """One host store behind whichever harness the incarnation runs, staged so
    ``spawn._reject_mount_hijack`` already refuses a caller aiming at it."""
    links = memory.memory_links()
    assert links, "claude declares a memory directory, so there is a pair"
    assert {staged for _, staged in links} == {memory.CONTAINER_STAGED_DIR}
    assert memory.CONTAINER_STAGED_DIR.startswith(f"{CONTAINER_MOUNT_STAGING_DIR}/")
    assert [declared for declared, _ in links] == [
        HARNESSES["claude"].memory_dir
    ]


def test_the_store_is_mounted_read_write(platform_root, tmp_path):
    """``rw`` is the feature: an unwritable store never accumulates."""
    flags = memory.mount_flags(tmp_path)
    assert flags[0] == "--mount-dir"
    assert flags[1] == f"{tmp_path}:{memory.CONTAINER_STAGED_DIR}:rw"


def test_measuring_a_store_that_does_not_exist_yet_is_empty(platform_root):
    """A fresh host answers the same as a store nothing has written to."""
    measurement = memory.measure()
    assert (measurement.files, measurement.bytes) == (0, 0)
    assert not measurement.truncated
    assert not measurement.large


def test_measurement_counts_nested_files_and_their_bytes(platform_root):
    directory = memory.prepare_memory_dir()
    (directory / "MEMORY.md").write_text("x" * 10, encoding="utf-8")
    nested = directory / "notes"
    nested.mkdir()
    (nested / "one.md").write_text("y" * 5, encoding="utf-8")
    measurement = memory.measure()
    assert measurement.files == 2
    assert measurement.bytes == 15


def test_a_symlink_in_the_store_is_not_followed(platform_root, tmp_path):
    """An agent-driven container can plant one; following it would report
    somebody else's file as memory."""
    outside = tmp_path / "secret.md"
    outside.write_text("z" * 500, encoding="utf-8")
    directory = memory.prepare_memory_dir()
    (directory / "link.md").symlink_to(outside)
    measurement = memory.measure()
    assert (measurement.files, measurement.bytes) == (0, 0)


def test_a_pathological_store_measures_truncated_rather_than_endlessly(
    platform_root, monkeypatch
):
    """A status poll runs this, so the walk is bounded and says when it stopped."""
    monkeypatch.setattr(memory, "MEASURE_ENTRY_CAP", 3)
    directory = memory.prepare_memory_dir()
    for index in range(6):
        (directory / f"m{index}.md").write_text("x", encoding="utf-8")
    measurement = memory.measure()
    assert measurement.truncated
    assert measurement.files == 3


def test_a_store_the_walk_finished_is_never_reported_truncated(
    platform_root, monkeypatch
):
    """A store sitting exactly on the boundary is exact, not "at least"."""
    monkeypatch.setattr(memory, "MEASURE_ENTRY_CAP", 3)
    directory = memory.prepare_memory_dir()
    for index in range(3):
        (directory / f"m{index}.md").write_text("x", encoding="utf-8")
    measurement = memory.measure()
    assert measurement.files == 3
    assert not measurement.truncated, "nothing was left unlooked-at"


def test_the_cap_bounds_the_walk_and_not_only_the_files_found(
    platform_root, monkeypatch
):
    """An empty directory costs the same syscall as a file, so a cap on files
    bounded nothing (review of !263: 5000 empty dirs, half a second)."""
    monkeypatch.setattr(memory, "MEASURE_ENTRY_CAP", 4)
    directory = memory.prepare_memory_dir()
    for index in range(20):
        (directory / f"d{index}").mkdir()
    measurement = memory.measure()
    assert measurement.files == 0
    assert measurement.truncated, "the walk stopped with directories unvisited"


def test_a_store_this_user_cannot_write_is_refused_rather_than_reported_ready(
    platform_root, monkeypatch, caplog
):
    """``ensure_state_dir`` only clears bits *outside* the state mode, so a narrow
    pre-existing store comes back untouched and mounting it would hand the harness
    a directory it cannot save into.

    Provoked through ``os.access`` rather than a mode bit for the reason
    ``conftest.denied_create`` gives: CI runs as root, where 0500 is writable."""
    real_access = memory.os.access
    directory = memory.memory_dir()

    def guarded(path, mode, **kwargs):
        if Path(path) == directory and mode & os.W_OK:
            return False
        return real_access(path, mode, **kwargs)

    monkeypatch.setattr(memory.os, "access", guarded)
    with caplog.at_level(logging.WARNING, logger="lmer_platform.memory"):
        assert memory.prepare_memory_dir() is None
    assert any(
        "platform_assistant_memory_store_unwritable" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores the mode bit this test relies on"
)
def test_the_refusal_holds_against_a_real_narrow_store(platform_root):
    """The same case with the kernel enforcing it, not a patched ``os.access``."""
    directory = memory.memory_dir()
    directory.mkdir(parents=True)
    directory.chmod(0o500)
    try:
        assert memory.prepare_memory_dir() is None
    finally:
        directory.chmod(0o700)


def test_a_large_store_warns_and_records_an_event_and_keeps_every_file(
    platform_root, caplog
):
    """Observed, never bounded: it says so and touches nothing."""
    directory = memory.prepare_memory_dir()
    (directory / "huge.md").write_text("x" * (memory.WARN_BYTES + 1), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="lmer_platform.memory"):
        measurement = memory.observe()
    assert measurement.large
    assert any(
        "platform_assistant_memory_large" in record.getMessage()
        for record in caplog.records
    )
    assert any(
        event.get("type") == "assistant_memory_large"
        for event in store.read_events()
    )
    assert (directory / "huge.md").is_file(), "nothing is ever deleted here"


def test_a_store_the_assistant_is_curating_is_not_worth_an_event(platform_root):
    directory = memory.prepare_memory_dir()
    (directory / "MEMORY.md").write_text("- one line\n", encoding="utf-8")
    measurement = memory.observe()
    assert not measurement.large
    assert not [
        event for event in store.read_events()
        if event.get("type") == "assistant_memory_large"
    ]
