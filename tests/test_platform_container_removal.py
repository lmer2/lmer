"""The platform's container removals: one background worker, a removal that is
not cut short by the query timeout, podman's half-removed containers, and the
sweep that clears them. Against tests/fake_container_runtime.py."""

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lmer_cli import container_follow, runtime
from lmer_platform import containers, daemon, registry, store
from lmer_platform.detect import Detector
from tests.conftest import strip_lmer_env
from tests.timing_budgets import SETTLE, stretch, wait_until

FAKE = Path(__file__).with_name("fake_container_runtime.py")
#: The real sweep: tests/conftest.py stubs it for every other test.
SWEEP = containers.sweep


@pytest.fixture
def fake(tmp_path, monkeypatch):
    strip_lmer_env(monkeypatch)
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "docker"
    shim.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE} "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv(runtime.RUNTIME_ENV, "docker")
    container_follow._supports_sig_proxy.cache_clear()
    state = tmp_path / "containers"
    monkeypatch.setenv("FAKE_RUNTIME_STATE", str(state))
    monkeypatch.setenv("FAKE_RUNTIME_DEADLINE", str(stretch(60.0)))
    log = tmp_path / "runtime.log"
    monkeypatch.setenv("FAKE_RUNTIME_LOG", str(log))
    monkeypatch.setattr(containers, "_WARNED", set())
    monkeypatch.setattr(containers, "sweep", SWEEP)
    assert containers.wait_for_removals(timeout=SETTLE)
    yield SimpleNamespace(state=state, tmp=tmp_path, verbs=lambda: _verbs(log))
    # Nothing queued by one test may run in the next one's runtime.
    assert containers.wait_for_removals(timeout=SETTLE)


def _verbs(log):
    try:
        return [json.loads(line) for line in log.read_text().splitlines()]
    except OSError:
        return []


def labelled(fake, session_id, script="exit 0", *, start=True):
    cid_file = fake.tmp / f"cid-{time.monotonic_ns()}"
    labels = runtime.container_labels(
        repo_host=None, repo_project=None, run_slug=None, platform_session=session_id,
        platform_instance=containers.platform_instance(),
    )
    subprocess.run(["docker", "create", *labels, "--cidfile", str(cid_file),
                    "fake-image", "/bin/sh", "-c", script], check=True)
    cid = cid_file.read_text()
    if start:
        subprocess.run(["docker", "start", cid], check=True)
        assert wait_until(lambda: (fake.state / cid / "exit").exists() or "sleep" in script,
                          budget=SETTLE)
    return cid


def half_removed(fake, cid):
    """What podman leaves when ``rm`` is killed partway: listed as ``removing``,
    its storage gone, so ``inspect`` and ``stop`` fail with 125."""
    (fake.state / cid / "removing").touch()
    (fake.state / cid / "storage-gone").touch()


def rm_calls(fake, cid):
    return [v for v in fake.verbs() if v[:1] == ["rm"] and v[-1] == cid]


def platform_warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == "lmer_platform.containers" and r.levelno >= logging.WARNING]


def test_a_removal_slower_than_the_query_timeout_is_not_killed(fake, monkeypatch):
    monkeypatch.setattr(containers, "QUERY_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setenv("FAKE_RUNTIME_RM_DELAY", "1.5")
    cid = labelled(fake, "s-1")
    containers.remove(cid)
    assert containers.wait_for_removals(timeout=SETTLE)
    assert not (fake.state / cid).exists()


def test_a_removal_cut_short_is_left_removing_and_the_sweep_clears_it(
    fake, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger="lmer_platform.containers")
    monkeypatch.setattr(containers, "REMOVE_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setenv("FAKE_RUNTIME_RM_DELAY", "5")
    cid = labelled(fake, "s-1")
    containers.remove(cid)
    assert containers.wait_for_removals(timeout=SETTLE)
    assert (fake.state / cid / "removing").exists()
    assert any(f"platform_container_remove_failed id={cid}" in m for m in platform_warnings(caplog))
    # One budget for the removal, retry included: a timed-out attempt is not
    # followed by a second as long.
    assert len(rm_calls(fake, cid)) == 1

    monkeypatch.delenv("FAKE_RUNTIME_RM_DELAY")
    containers.sweep()
    assert containers.wait_for_removals(timeout=SETTLE)
    assert not (fake.state / cid).exists()
    assert f"platform_container_swept id={cid}" in caplog.text


def test_an_rm_that_fails_once_is_retried(fake, monkeypatch, caplog):
    counter = fake.tmp / "rm-failures"
    counter.write_text("1")
    monkeypatch.setenv("FAKE_RUNTIME_RM_FAIL", str(counter))
    cid = labelled(fake, "s-1")
    containers.remove(cid)
    assert containers.wait_for_removals(timeout=SETTLE)
    assert not (fake.state / cid).exists()
    assert len(rm_calls(fake, cid)) == 2
    assert platform_warnings(caplog) == []


def test_an_rm_that_keeps_failing_is_logged_with_the_container_id(fake, monkeypatch, caplog):
    counter = fake.tmp / "rm-failures"
    counter.write_text("5")
    monkeypatch.setenv("FAKE_RUNTIME_RM_FAIL", str(counter))
    cid = labelled(fake, "s-1")
    containers.remove(cid)
    assert containers.wait_for_removals(timeout=SETTLE)
    assert (fake.state / cid).exists()
    [warning] = platform_warnings(caplog)
    assert f"platform_container_remove_failed id={cid}" in warning


def test_callers_never_wait_and_one_container_is_removed_once(fake, monkeypatch):
    monkeypatch.setenv("FAKE_RUNTIME_RM_DELAY", str(stretch(4.0)))
    cid = labelled(fake, "s-1")
    started = time.monotonic()
    containers.remove(cid)
    containers.remove(cid)
    containers.stop_container(cid)
    containers.remove_for_session("s-1")
    assert time.monotonic() - started < 1.0
    assert containers.wait_for_removals(timeout=SETTLE)
    assert not (fake.state / cid).exists()
    assert len(rm_calls(fake, cid)) == 1


def test_a_stop_does_not_wait_behind_a_slow_removal(fake, monkeypatch):
    """A container that has to stop (an old assistant beside its successor)
    must not wait for an unrelated removal on a slow disk."""
    monkeypatch.setenv("FAKE_RUNTIME_RM_DELAY", str(stretch(20.0)))
    slow = labelled(fake, "s-slow")
    live = labelled(fake, "s-live", "sleep 60")
    assert wait_until(lambda: (fake.state / live / "pid").exists(), budget=SETTLE)
    containers.remove(slow)
    assert wait_until(lambda: (fake.state / slow / "removing").exists(), budget=SETTLE)
    containers.stop_container(live)
    assert wait_until(lambda: (fake.state / live / "exit").exists()
                      or not (fake.state / live).exists(), budget=SETTLE)
    assert (fake.state / slow / "removing").exists(), "the slow removal already ended"
    monkeypatch.delenv("FAKE_RUNTIME_RM_DELAY")


def test_removals_run_one_at_a_time_on_one_thread(fake, monkeypatch):
    monkeypatch.setenv("FAKE_RUNTIME_RM_DELAY", "0.5")
    first, second = labelled(fake, "s-1"), labelled(fake, "s-2")
    seen = []
    real = containers._rm

    def recording(runtime_name, container_id):
        seen.append(("start", container_id, threading.current_thread().name))
        real(runtime_name, container_id)
        seen.append(("end", container_id, threading.current_thread().name))

    monkeypatch.setattr(containers, "_rm", recording)
    containers.remove(first)
    assert wait_until(lambda: seen, budget=SETTLE)
    containers.remove(second)
    assert containers.wait_for_removals(timeout=SETTLE)
    assert [(step, cid) for step, cid, _ in seen] == [
        ("start", first), ("end", first), ("start", second), ("end", second),
    ]
    assert {name for _, _, name in seen} == {"lmer-platform-container-remover"}


def test_a_container_whose_stop_fails_is_still_removed(fake, caplog):
    """podman refuses to stop a container in ``removing``; ``rm --force`` is what
    clears it, and the stop failing is not worth a warning."""
    cid = labelled(fake, "s-1")
    half_removed(fake, cid)
    containers.stop_for_session("s-1")
    assert containers.wait_for_removals(timeout=SETTLE)
    assert not (fake.state / cid).exists()
    assert platform_warnings(caplog) == []


def test_the_sweep_removes_half_removed_containers_of_ended_sessions_only(fake, caplog):
    caplog.set_level(logging.INFO, logger="lmer_platform.containers")
    ended = labelled(fake, "s-ended")
    owned = labelled(fake, "s-live")
    unrecorded = labelled(fake, "s-unrecorded")
    healthy = labelled(fake, "s-healthy")
    for cid in (ended, owned, unrecorded):
        half_removed(fake, cid)
    # This process stands in for the launchers still running their own `rm`;
    # one of them never reported its container id.
    registry.register("s-live", pid=os.getpid(), log_path=str(fake.tmp / "live.log"))
    registry.update("s-live", container_id=owned)
    registry.register("s-unrecorded", pid=os.getpid(), log_path=str(fake.tmp / "u.log"))

    containers.sweep()
    assert containers.wait_for_removals(timeout=SETTLE)
    assert not (fake.state / ended).exists()
    assert (fake.state / owned).exists() and (fake.state / unrecorded).exists()
    assert (fake.state / healthy).exists()
    assert f"platform_container_swept id={ended}" in caplog.text
    assert platform_warnings(caplog) == []


def test_the_sweep_spares_a_container_whose_exit_is_not_recorded(fake, monkeypatch):
    """Its launcher died, but the container may hold the only copy of the exit
    code: the next daemon start records it before removing the container."""
    cid = labelled(fake, "s-crashed")
    registry.register("s-crashed", pid=2**22, log_path=str(fake.tmp / "c.log"))
    monkeypatch.setenv("FAKE_RUNTIME_FAIL_INSPECT", "1")
    containers.sweep()
    assert containers.wait_for_removals(timeout=SETTLE)
    assert (fake.state / cid).exists()
    monkeypatch.delenv("FAKE_RUNTIME_FAIL_INSPECT")


def _unreadable(monkeypatch, session_id):
    """The registry entry of *session_id* raises on read, as an OSError or a
    newer daemon's schema would; every other entry reads as usual."""
    real = registry.read_json
    target = registry.session_path(session_id)

    def read(path, *args, **kwargs):
        if Path(path) == target:
            raise store.StoreError(f"cannot read {target.name} (injected)")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(registry, "read_json", read)
    return lambda: monkeypatch.setattr(registry, "read_json", real)


def test_the_sweep_removes_nothing_while_the_registry_cannot_be_read_in_full(
    fake, monkeypatch, caplog
):
    """A session missing from the read is not a session that ended: its
    launcher may be running its own `rm` on the container right now."""
    owned = labelled(fake, "s-live")
    ended = labelled(fake, "s-ended")
    for cid in (owned, ended):
        half_removed(fake, cid)
    registry.register("s-live", pid=os.getpid(), log_path=str(fake.tmp / "live.log"))
    registry.update("s-live", container_id=owned)
    readable = _unreadable(monkeypatch, "s-live")

    for _ in range(2):
        containers.sweep()
        assert containers.wait_for_removals(timeout=SETTLE)
    assert (fake.state / owned).exists() and (fake.state / ended).exists()
    assert rm_calls(fake, owned) == [] and rm_calls(fake, ended) == []
    [warning] = platform_warnings(caplog)
    assert "platform_container_sweep_failed" in warning and "injected" in warning

    readable()
    containers.sweep()
    assert containers.wait_for_removals(timeout=SETTLE)
    assert (fake.state / owned).exists()
    assert not (fake.state / ended).exists()


def test_the_sweep_fails_closed_when_the_registry_cannot_be_listed(fake):
    """Through the real listing: a regular file where the directory belongs
    fails ``os.scandir`` with ENOTDIR for root too, unlike a mode-000 directory."""
    cid = labelled(fake, "s-1")
    half_removed(fake, cid)
    directory = store.sessions_dir()
    directory.parent.mkdir(parents=True, exist_ok=True)
    if directory.is_dir():
        directory.rmdir()
    directory.write_text("")

    for _ in range(2):
        containers.sweep()
        assert containers.wait_for_removals(timeout=SETTLE)
    assert (fake.state / cid).exists()
    with pytest.raises(store.StoreError):
        registry.list_sessions(live_only=False, strict=True)
    with pytest.raises(store.StoreError):
        registry.set_aside_ids()
    assert registry.list_sessions(live_only=False) == []


def test_the_sweep_treats_a_sessions_directory_not_made_yet_as_empty(fake):
    cid = labelled(fake, "s-ended")
    half_removed(fake, cid)
    assert not store.sessions_dir().exists()

    containers.sweep()
    assert containers.wait_for_removals(timeout=SETTLE)
    assert not (fake.state / cid).exists()


def test_the_sweep_spares_a_session_whose_entry_was_set_aside_as_corrupt(fake):
    """read_json moves a corrupt entry aside, so the next read is complete
    again without it; the session it named may still be running."""
    owned = labelled(fake, "s-live")
    ended = labelled(fake, "s-ended")
    for cid in (owned, ended):
        half_removed(fake, cid)
    registry.register("s-live", pid=os.getpid(), log_path=str(fake.tmp / "live.log"))
    registry.update("s-live", container_id=owned)
    registry.session_path("s-live").write_text("{not json")

    for _ in range(2):
        containers.sweep()
        assert containers.wait_for_removals(timeout=SETTLE)
    assert not registry.session_path("s-live").exists(), "the entry was not set aside"
    assert registry.set_aside_ids() == {"s-live"}
    assert (fake.state / owned).exists()
    assert not (fake.state / ended).exists()


def test_a_queued_sweep_removal_checks_the_registry_in_full_again(fake, monkeypatch):
    cid = labelled(fake, "s-live")
    half_removed(fake, cid)
    registry.register("s-live", pid=os.getpid(), log_path=str(fake.tmp / "live.log"))
    _unreadable(monkeypatch, "s-live")
    containers._sweep_remove("docker", containers._platform_filter(), cid)
    assert (fake.state / cid).exists()
    assert rm_calls(fake, cid) == []


def test_the_platform_rm_runs_in_a_session_of_its_own(monkeypatch):
    """A Ctrl-C or service stop aimed at the daemon must not reach a removal
    halfway, which is what leaves a container in ``removing``."""
    seen = []

    def run(args, **kwargs):
        seen.append((args[1], kwargs.get("start_new_session")))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(containers.subprocess, "run", run)
    containers._rm("docker", "c1")
    containers._kill("docker", "c1")
    assert seen == [("rm", True), ("kill", False)]


# --- the worker --------------------------------------------------------------

def _blocker(remover, key=("block",), *, conditional=False):
    """Queue a job on *remover* that holds the worker until released."""
    started, release = threading.Event(), threading.Event()

    def hold():
        started.set()
        release.wait(SETTLE)

    assert remover.submit(key, hold, conditional=conditional)
    assert started.wait(SETTLE)
    return release


def test_an_unconditional_removal_replaces_a_queued_sweep_job():
    """A sweep job re-checks and may do nothing; dropping an exit's removal
    as its duplicate would leave the container behind."""
    remover = containers._Remover()
    ran = []
    release = _blocker(remover)
    assert remover.submit("c1", ran.append, "sweep", conditional=True)
    assert remover.submit("c1", ran.append, "rm")
    assert not remover.submit("c1", ran.append, "rm again")
    assert not remover.submit("c1", ran.append, "sweep again", conditional=True)
    release.set()
    assert remover.wait_idle(SETTLE)
    assert ran == ["rm"]


def test_an_unconditional_removal_queues_behind_a_running_sweep_job():
    remover = containers._Remover()
    ran = []
    release = _blocker(remover, "c1", conditional=True)
    assert remover.submit("c1", ran.append, "rm")
    assert not remover.submit("c1", ran.append, "sweep", conditional=True)
    release.set()
    assert remover.wait_idle(SETTLE)
    assert ran == ["rm"]


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_worker_ended_by_a_job_is_restarted_by_the_next_submit():
    """Even when that submit is a duplicate of a stranded job: otherwise a
    stranded sweep blocks every later sweep for good."""
    remover = containers._Remover()
    ran = []
    started, release = threading.Event(), threading.Event()

    def end_the_worker():
        started.set()
        release.wait(SETTLE)
        raise SystemExit

    assert remover.submit("bad", end_the_worker)
    assert started.wait(SETTLE)
    assert remover.submit(("sweep",), ran.append, "sweep")
    release.set()
    assert wait_until(lambda: not remover._thread.is_alive(), budget=SETTLE)
    assert ran == []

    assert not remover.submit(("sweep",), ran.append, "duplicate")
    assert remover.wait_idle(SETTLE)
    assert ran == ["sweep"]


def test_a_stop_thread_that_cannot_start_does_not_leave_the_queue_busy(monkeypatch):
    remover = containers._Remover()
    real_start = threading.Thread.start

    def refuse(thread):
        if thread.name == "lmer-platform-container-stop":
            raise RuntimeError("can't start new thread")
        return real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", refuse)
    remover.halt(lambda: pytest.fail("ran without a thread"))
    assert remover.wait_idle(0)


def test_a_queued_sweep_removal_checks_again_before_acting(fake):
    """The runtime may have recovered while the job waited its turn."""
    cid = labelled(fake, "s-1")
    containers._sweep_remove("docker", containers._platform_filter(), cid)
    assert (fake.state / cid).exists()
    half_removed(fake, cid)
    containers._sweep_remove("docker", containers._platform_filter(), cid)
    assert not (fake.state / cid).exists()


def test_the_sweep_spares_a_running_container_it_cannot_inspect(fake, monkeypatch):
    """A runtime that cannot answer is not a half-removed container."""
    cid = labelled(fake, "s-1", "sleep 30")
    assert wait_until(lambda: (fake.state / cid / "pid").exists(), budget=SETTLE)
    monkeypatch.setenv("FAKE_RUNTIME_FAIL_INSPECT", "1")
    containers.sweep()
    assert containers.wait_for_removals(timeout=SETTLE)
    assert (fake.state / cid).exists()
    monkeypatch.delenv("FAKE_RUNTIME_FAIL_INSPECT")
    subprocess.run(["docker", "rm", "--force", cid], check=True)


def test_a_sweep_that_cannot_remove_warns_once_per_container(fake, monkeypatch, caplog):
    cid = labelled(fake, "s-1")
    half_removed(fake, cid)
    counter = fake.tmp / "rm-failures"
    counter.write_text("100")
    monkeypatch.setenv("FAKE_RUNTIME_RM_FAIL", str(counter))
    for _ in range(3):
        containers.sweep()
        assert containers.wait_for_removals(timeout=SETTLE)
    assert len(rm_calls(fake, cid)) == 6
    [warning] = platform_warnings(caplog)
    assert f"platform_container_sweep_remove_failed id={cid}" in warning
    monkeypatch.delenv("FAKE_RUNTIME_RM_FAIL")


def test_discovery_warns_once_about_an_unreadable_container(fake, caplog):
    cid = labelled(fake, "s-1")
    half_removed(fake, cid)
    assert containers.discover() == []
    assert containers.discover() == []
    [warning] = platform_warnings(caplog)
    assert f"platform_container_unreadable id={cid}" in warning


def test_the_detector_sweeps_at_its_first_tick_and_then_on_its_interval():
    calls = []
    detector = Detector(
        SimpleNamespace(), container_sweep=lambda: calls.append(time.monotonic()),
        container_sweep_interval=3600,
    )
    detector._container_sweep_stage()
    detector._container_sweep_stage()
    assert len(calls) == 1
    detector._container_swept_at -= 3600
    detector._container_sweep_stage()
    assert len(calls) == 2


def test_a_detector_built_without_a_sweep_never_reaches_a_runtime(monkeypatch):
    monkeypatch.setattr(containers, "sweep", lambda: pytest.fail("swept"))
    Detector(SimpleNamespace())._container_sweep_stage()


def test_the_daemon_hands_the_detector_the_sweep(monkeypatch):
    built = {}

    class Recorder:
        notice = ""

        def __init__(self, config, **kwargs):
            built.update(kwargs)

        def start(self):
            pass

    monkeypatch.setattr(daemon, "Detector", Recorder)
    daemon._watch_for_attention(SimpleNamespace())
    assert built["container_sweep"] is containers.sweep
    assert built["container_sweep_interval"] == containers.SWEEP_INTERVAL_SECONDS == 600
    assert containers.REMOVE_TIMEOUT_SECONDS == 30 * 60
