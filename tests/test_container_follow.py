"""Following a detached session container (#444), against a fake runtime that
keeps containers as detached processes (tests/fake_container_runtime.py)."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lmer_cli import container_follow, runtime
from lmer_cli.container import clone_and_exec
from tests.timing_budgets import CLI, JOIN, SETTLE, stretch, wait_until

FAKE = Path(__file__).with_name("fake_container_runtime.py")
#: Outlasts the longest container these tests run (``sleep 30``), so only a
#: hang in the fake reaches it.
RUNTIME_DEADLINE = stretch(60.0)


@pytest.fixture
def fake_runtime(tmp_path, monkeypatch):
    """``docker`` and ``podman`` on PATH are the fake; returns a helper that
    creates containers."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # podman too, so a real one on the host cannot answer the --sig-proxy probe.
    for name in ("docker", "podman"):
        shim = bin_dir / name
        shim.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE} "$@"\n')
        shim.chmod(0o755)
    state = tmp_path / "containers"
    monkeypatch.setenv("FAKE_RUNTIME_STATE", str(state))
    monkeypatch.setenv("FAKE_RUNTIME_DEADLINE", str(RUNTIME_DEADLINE))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    # Cached per runtime name; each test's PATH may answer differently.
    container_follow._supports_sig_proxy.cache_clear()

    def create(script, *, labels=()):
        cid_file = tmp_path / f"cid-{time.monotonic_ns()}"
        subprocess.run(
            ["docker", "create", *labels, "--cidfile", str(cid_file),
             "fake-image", "/bin/sh", "-c", script],
            check=True,
        )
        return cid_file.read_text()

    create.state = state
    return create


def both(capfd):
    """Container output (stdout) and lmer's own lines (stderr, since stdout is
    not a terminal here)."""
    captured = capfd.readouterr()
    return captured.out + captured.err


def remove_once_running(state, cid):
    """``docker rm --force`` *cid* once its process is up, from a thread.
    Returns a join that says whether it was up: a fixed delay raced the start
    under load (#636). The removal runs either way, so a follower never hangs
    on a container that never came up."""
    up = []

    def remove():
        up.append(wait_until(lambda: (state / cid / "pid").exists(), budget=SETTLE))
        subprocess.run(["docker", "rm", "--force", cid], check=False)

    thread = threading.Thread(target=remove, daemon=True)
    thread.start()

    def removed_while_running():
        thread.join(JOIN)
        return up == [True]

    return removed_while_running


def follow(cid, **kwargs):
    kwargs.setdefault("started", False)
    kwargs.setdefault("interactive", False)
    return container_follow.follow("docker", cid, **kwargs)


def test_the_real_exit_code_is_returned_announced_and_the_container_removed(
    fake_runtime, capfd
):
    cid = fake_runtime("echo hello; echo why >&2; exit 3")
    assert follow(cid) == 3
    captured = capfd.readouterr()
    assert "hello" in captured.out and "why" in captured.out
    # lmer's own line stays out of a headless run's stdout.
    assert "container exited with code 3" in captured.err
    assert "container exited" not in captured.out
    assert not (fake_runtime.state / cid).exists()


def test_a_dead_attach_client_does_not_end_the_session(fake_runtime, capfd):
    """The #444 mechanism, inverted: the console going away costs nothing, and a
    later follower still gets the code."""
    cid = fake_runtime("sleep 0.5; echo still here; exit 7")
    subprocess.run(["docker", "start", cid], check=True)  # detached, nobody attached
    assert follow(cid, started=True) == 7
    assert "container exited with code 7" in both(capfd)


def test_a_lost_console_keeps_waiting_rather_than_reporting_success(
    fake_runtime, capfd, monkeypatch
):
    """A headless run whose attach broke, or a terminal that hung up: the code
    still has to reach someone."""
    monkeypatch.delenv(container_follow.PLATFORM_SESSION_ENV, raising=False)
    cid = fake_runtime("sleep 0.5; exit 6")
    subprocess.run(["docker", "start", cid], check=True)
    monkeypatch.setattr(container_follow, "_console_command", lambda *a, **k: ["false"])
    assert follow(cid, started=True, interactive=True) == 6  # no terminal here
    headless = fake_runtime("sleep 0.5; exit 2")
    subprocess.run(["docker", "start", headless], check=True)
    assert follow(headless, started=True, interactive=False) == 2


def test_a_start_that_never_ran_is_125_and_cleaned_up(fake_runtime, monkeypatch):
    """Where a runtime refuses a GPU or a port at start; `wait` on a container
    that never ran would block forever."""
    cid = fake_runtime("exit 0")
    monkeypatch.setattr(container_follow, "_console_command", lambda *a, **k: ["false"])
    assert follow(cid) == 125
    assert not (fake_runtime.state / cid).exists()


def test_sigterm_before_the_start_removes_the_container(fake_runtime, monkeypatch):
    cid = fake_runtime("sleep 30")
    monkeypatch.setattr(
        container_follow, "_backfill",
        lambda *a: os.kill(os.getpid(), signal.SIGTERM),
    )
    assert follow(cid, since="2026-10-08T00:00:00Z") == 143
    assert not (fake_runtime.state / cid).exists()


def test_a_follower_whose_container_was_removed_reads_the_exit_file(
    fake_runtime, capfd, monkeypatch, tmp_path
):
    """Two followers after a restart: whichever removed the container left the
    code in the exit file."""
    exit_file = tmp_path / "exit.json"
    monkeypatch.setenv(container_follow.EXIT_FILE_ENV, str(exit_file))
    cid = fake_runtime("sleep 30")
    subprocess.run(["docker", "start", cid], check=True)
    exit_file.write_text(json.dumps({"exit_code": 9, "container": cid}))
    threading.Timer(0.5, subprocess.run, (["docker", "rm", "--force", cid],)).start()
    assert follow(cid, started=True) == 9


def test_an_exit_file_for_another_container_is_ignored(
    fake_runtime, capfd, monkeypatch, tmp_path
):
    exit_file = tmp_path / "exit.json"
    monkeypatch.setenv(container_follow.EXIT_FILE_ENV, str(exit_file))
    cid = fake_runtime("sleep 30")
    exit_file.write_text(json.dumps({"exit_code": 9, "container": "someone-else"}))
    removed_while_running = remove_once_running(fake_runtime.state, cid)
    assert follow(cid) != 9
    assert removed_while_running()


def test_a_replay_starts_where_the_follower_says(fake_runtime, capfd):
    cid = fake_runtime("echo old news; sleep 0.5")
    subprocess.run(["docker", "start", cid], check=True)
    time.sleep(0.2)
    assert follow(cid, started=True, since="2999-01-01T00:00:00Z") == 0
    assert "old news" not in both(capfd)


def test_a_follower_replays_what_it_missed(fake_runtime, capfd):
    cid = fake_runtime("echo before anyone attached; sleep 0.5")
    subprocess.run(["docker", "start", cid], check=True)
    time.sleep(0.2)
    assert follow(cid, started=True, since="2026-10-08T00:00:00Z") == 0
    assert "before anyone attached" in both(capfd)


def test_a_log_driver_that_cannot_replay_leaves_a_note_not_a_failure(
    fake_runtime, capfd, monkeypatch
):
    monkeypatch.setenv("FAKE_RUNTIME_NO_LOGS", "1")
    cid = fake_runtime("sleep 0.3; exit 0")
    subprocess.run(["docker", "start", cid], check=True)
    assert follow(cid, started=True, since="2026-10-08T00:00:00Z") == 0
    assert "could not be recovered" in both(capfd)


def test_sigterm_stops_the_container_and_still_records_its_exit(fake_runtime, capfd):
    cid = fake_runtime("trap 'exit 143' TERM; while true; do sleep 0.1; done")
    threading.Timer(1.0, os.kill, (os.getpid(), signal.SIGTERM)).start()
    assert follow(cid) == 143
    assert not (fake_runtime.state / cid).exists()


def test_a_container_that_vanished_is_not_reported_as_a_clean_exit(fake_runtime, capfd):
    cid = fake_runtime("sleep 30")
    removed_while_running = remove_once_running(fake_runtime.state, cid)
    assert follow(cid) != 0
    assert "exited with code unknown" in both(capfd)
    assert removed_while_running()


def test_the_fake_start_of_a_removed_container_fails_rather_than_hangs(fake_runtime):
    """The fake's start used to wait forever for a pid that a container removed
    before it ran never publishes (#636)."""
    cid = fake_runtime("sleep 30")
    subprocess.run(["docker", "rm", "--force", cid], check=True)
    result = subprocess.run(
        ["docker", "start", "--attach", cid],
        capture_output=True, text=True, timeout=CLI, check=False,
    )
    assert result.returncode == 1
    assert f"no such container: {cid}" in result.stderr


def test_a_fake_wait_past_its_deadline_exits_124(fake_runtime):
    """A hang in the fake fails its test rather than stalling the gate (#636)."""
    cid = fake_runtime("sleep 30")
    subprocess.run(["docker", "start", cid], check=True)
    try:
        result = subprocess.run(
            ["docker", "wait", cid], capture_output=True, text=True, timeout=CLI,
            env={**os.environ, "FAKE_RUNTIME_DEADLINE": "0.5"}, check=False,
        )
    finally:
        container_follow.remove("docker", cid)
    assert result.returncode == 124
    assert "FAKE_RUNTIME_DEADLINE" in result.stderr


def test_console_commands_carry_the_detach_keys(fake_runtime):
    command = container_follow._console_command(
        "podman", "c1", started=False, interactive=True
    )
    assert command == [
        "podman", "start", "--attach", "--interactive",
        "--detach-keys", runtime.DETACH_KEYS, "c1",
    ]
    # Asked of the runtime: the fake answers as docker does (for podman too),
    # whose attach takes --sig-proxy and whose start does not.
    assert container_follow._console_command(
        "docker", "c1", started=True, interactive=False
    ) == ["docker", "attach", "--no-stdin", "--sig-proxy=false",
          "--detach-keys", runtime.DETACH_KEYS, "c1"]
    assert "--sig-proxy=false" not in container_follow._console_command(
        "docker", "c1", started=False, interactive=True
    )
    assert runtime.DETACH_KEYS == "ctrl-],ctrl-\\"


def test_a_cli_detach_hands_the_terminal_back_and_leaves_the_container(
    fake_runtime, capfd, monkeypatch
):
    monkeypatch.delenv(container_follow.PLATFORM_SESSION_ENV, raising=False)
    cid = fake_runtime("sleep 30")
    subprocess.run(["docker", "start", cid], check=True)
    # A console that ends while the container runs and the terminal is still
    # there: what a detach looks like.
    monkeypatch.setattr(container_follow, "_console_command", lambda *a, **k: ["true"])
    monkeypatch.setattr(container_follow, "_terminal_alive", lambda: True)
    try:
        assert follow(cid, started=True, interactive=True) == 0
        assert f"lmer follow {cid[:12]}" in both(capfd)
        assert container_follow.is_running("docker", cid)
    finally:
        container_follow.remove("docker", cid)


def test_a_platform_follower_waits_through_a_detach(fake_runtime, capfd, monkeypatch):
    """The platform needs the exit code from it."""
    monkeypatch.setenv(container_follow.PLATFORM_SESSION_ENV, "s-1")
    cid = fake_runtime("sleep 0.5; exit 4")
    subprocess.run(["docker", "start", cid], check=True)
    monkeypatch.setattr(container_follow, "_console_command", lambda *a, **k: ["true"])
    assert follow(cid, started=True) == 4


def test_a_console_that_ends_with_an_error_hands_back_with_its_code(
    fake_runtime, capfd, monkeypatch
):
    """`docker attach` exits nonzero on a detach, `docker start --attach` 0, so
    the code cannot decide; it goes into the notice instead."""
    monkeypatch.delenv(container_follow.PLATFORM_SESSION_ENV, raising=False)
    cid = fake_runtime("sleep 30")
    subprocess.run(["docker", "start", cid], check=True)
    monkeypatch.setattr(container_follow, "_console_command", lambda *a, **k: ["false"])
    monkeypatch.setattr(container_follow, "_terminal_alive", lambda: True)
    try:
        assert follow(cid, started=True, interactive=True) == 0
        assert "console ended with code 1; the container keeps running" in both(capfd)
        assert container_follow.is_running("docker", cid)
    finally:
        container_follow.remove("docker", cid)


def test_ctrl_c_while_waiting_stops_the_container(fake_runtime, capfd, monkeypatch):
    cid = fake_runtime("trap 'exit 0' TERM; while true; do sleep 0.1; done")
    subprocess.run(["docker", "start", cid], check=True)
    real_wait = container_follow.wait_exit_code
    calls = []

    def interrupted_once(runtime, container):
        calls.append(container)
        if len(calls) == 1:
            raise KeyboardInterrupt
        return real_wait(runtime, container)

    monkeypatch.setattr(container_follow, "wait_exit_code", interrupted_once)
    monkeypatch.setattr(container_follow, "_console_command", lambda *a, **k: ["true"])
    assert follow(cid, started=True) == 0
    assert not (fake_runtime.state / cid).exists()


def test_a_graceful_stop_lets_the_container_finish_its_teardown(fake_runtime, capfd):
    """The session-end backstop runs in the container's TERM handling; the stop
    must wait for it rather than kill."""
    cid = fake_runtime(
        "trap 'sleep 1; echo teardown done; exit 0' TERM; while true; do sleep 0.1; done"
    )
    threading.Timer(1.0, os.kill, (os.getpid(), signal.SIGTERM)).start()
    assert follow(cid) == 0
    assert "teardown done" in both(capfd)


def test_a_start_the_runtime_refuses_is_125(fake_runtime, monkeypatch):
    cid = fake_runtime("exit 0")
    monkeypatch.setenv("FAKE_RUNTIME_FAIL_START", "1")
    assert follow(cid) == 125
    assert not (fake_runtime.state / cid).exists()


def test_sig_proxy_is_asked_of_each_verb(fake_runtime, monkeypatch):
    monkeypatch.setenv("FAKE_RUNTIME_FLAVOR", "podman")
    container_follow._supports_sig_proxy.cache_clear()
    assert "--sig-proxy=false" in container_follow._console_command(
        "docker", "c1", started=False, interactive=True
    )


def test_removal_kills_first(fake_runtime, monkeypatch, tmp_path):
    """podman's rm --force would otherwise honour the stop timeout."""
    log = tmp_path / "runtime.log"
    monkeypatch.setenv("FAKE_RUNTIME_LOG", str(log))
    cid = fake_runtime("sleep 30")
    subprocess.run(["docker", "start", cid], check=True)
    container_follow.remove("docker", cid)
    verbs = [json.loads(line)[0] for line in log.read_text().splitlines()]
    assert verbs[-2:] == ["kill", "rm"]


def test_follow_falls_through_for_a_container_it_cannot_inspect(
    fake_runtime, capfd, monkeypatch, tmp_path
):
    """Gone already — its old follower recorded the code and removed it."""
    monkeypatch.setenv(runtime.RUNTIME_ENV, "docker")
    exit_file = tmp_path / "exit.json"
    monkeypatch.setenv(container_follow.EXIT_FILE_ENV, str(exit_file))
    exit_file.write_text(json.dumps({"exit_code": 0, "container": "gone-cid"}))
    assert container_follow.main(["gone-cid"]) == 0


def test_a_detach_says_what_it_costs(fake_runtime, capfd, monkeypatch):
    monkeypatch.delenv(container_follow.PLATFORM_SESSION_ENV, raising=False)
    cid = fake_runtime("sleep 30")
    subprocess.run(["docker", "start", cid], check=True)
    monkeypatch.setattr(container_follow, "_console_command", lambda *a, **k: ["true"])
    monkeypatch.setattr(container_follow, "_terminal_alive", lambda: True)
    try:
        assert follow(cid, started=True, interactive=True, detach_note="thread ends") == 0
        assert "thread ends" in both(capfd)
    finally:
        container_follow.remove("docker", cid)


def test_an_exit_code_that_cannot_be_recorded_keeps_the_container(
    fake_runtime, capfd, monkeypatch, tmp_path
):
    """The runtime's copy is then the only one left."""
    monkeypatch.setenv(
        container_follow.EXIT_FILE_ENV, str(tmp_path / "missing-dir" / "exit.json")
    )
    cid = fake_runtime("exit 4")
    assert follow(cid) == 4
    assert (fake_runtime.state / cid).exists()


def test_follow_refuses_a_container_lmer_did_not_create(fake_runtime, capfd, monkeypatch):
    monkeypatch.setenv(runtime.RUNTIME_ENV, "docker")
    foreign = fake_runtime("sleep 30")
    managed = fake_runtime("exit 0", labels=["--label", "io.lmer.managed=1"])
    try:
        assert container_follow.main([foreign]) == 2
        assert (fake_runtime.state / foreign).exists()
        subprocess.run(["docker", "start", managed], check=True)
        assert container_follow.main([managed]) == 0
    finally:
        container_follow.remove("docker", foreign)


def test_a_stop_leaves_room_for_the_session_end_backstop():
    assert runtime.STOP_TIMEOUT_SECONDS > clone_and_exec.SESSION_END_TIMEOUT_SECONDS
    args = runtime.base_run_args("docker", False, "developer")
    assert args[args.index("--stop-timeout") + 1] == str(runtime.STOP_TIMEOUT_SECONDS)
