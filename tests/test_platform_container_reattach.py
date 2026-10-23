"""A session survives its daemon's death (#444): its container is detached and
labelled, a real daemon process spawns it and dies, the session prints, and a
fresh daemon finds the container by label, follows it again and records its
real exit code. Real processes and PTYs; the container runtime is the fake in
tests/fake_container_runtime.py."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lmer_cli import container_follow, runtime
from lmer_platform import config as cfg
from lmer_platform import containers, lifecycle, reattach, registry, spawn, store
from tests.conftest import strip_lmer_env

FAKE = Path(__file__).with_name("fake_container_runtime.py")
SRC = Path(spawn.__file__).resolve().parents[1]

#: Stands in for `lmer`: `follow` is the real thing, and a launch creates a
#: labelled container exactly as lmer does, then follows it with the real code.
LMER = f"""#!{sys.executable}
import os, subprocess, sys, tempfile
sys.path.insert(0, {str(SRC)!r})
from lmer_cli.cli import main
if sys.argv[1:2] == ["follow"]:
    sys.exit(main(sys.argv[1:]))
from pathlib import Path
from lmer_cli import cli, container_follow, runtime
cid_file = Path(tempfile.mkdtemp()) / "cid"
session = os.environ["LMER_PLATFORM_SESSION_ID"]
slug = os.environ["LMER_PLATFORM_RUN_SLUG"]
labels = runtime.container_labels(
    repo_host=None, repo_project=None, run_slug=slug, platform_session=session,
    platform_instance=os.environ["LMER_PLATFORM_INSTANCE"],
)
rc, cid, name = container_follow.create(
    "docker",
    ["docker", "create", *labels, "--name", runtime.container_name(slug, session),
     "--cidfile", str(cid_file), "fake-image",
     "/bin/sh", "-c", os.environ["FAKE_SESSION_SCRIPT"]],
    cid_file, rename=lambda: runtime.container_name(slug, session, retry=True),
)
assert rc == 0, rc
cli._record_platform_facts(container=dict(id=cid, name=name))
sys.exit(container_follow.follow("docker", cid, started=False,
                                 interactive=sys.stdin.isatty(), name=name))
"""

DAEMON = r"""
import json, os, sys
from lmer_platform import config as cfg, spawn, store
store.PLATFORM_DIR = sys.argv[1]
result = spawn.spawn_session(
    cfg.load({"lmer_bin": sys.argv[2]}),
    spawn.SpawnRequest(
        taskdef="develop",
        target="https://gitlab.example.com/agents/global/-/work_items/444",
        repo_url="https://gitlab.example.com/agents/global.git",
    ),
)
print(json.dumps({"session": result.session_id, "pid": result.pid}), flush=True)
os._exit(0)
"""


def end_with(control, code):
    """Let the session exit with *code*. Whole or not at all: the session script
    reads the file the moment it exists, and a half-written one exits with ""."""
    partial = control["end"].with_name("end.partial")
    partial.write_text(str(code))
    os.replace(partial, control["end"])


@pytest.fixture
def control(tmp_path):
    files = {"go": tmp_path / "go", "end": tmp_path / "end"}
    yield files
    if not files["end"].exists():
        end_with(files, 0)
    files["go"].touch()


@pytest.fixture
def platform_root(tmp_path, monkeypatch, control):
    strip_lmer_env(monkeypatch)
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "docker"
    shim.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE} "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv(runtime.RUNTIME_ENV, "docker")
    container_follow._supports_sig_proxy.cache_clear()
    monkeypatch.setenv("FAKE_RUNTIME_STATE", str(tmp_path / "containers"))
    # Waits on files rather than sleeping, so nothing races a timer; exits with
    # whatever code the test writes.
    monkeypatch.setenv("FAKE_SESSION_SCRIPT", (
        'echo "session started"; '
        f'while [ ! -e "{control["go"]}" ]; do sleep 0.05; done; '
        'echo "printed after the daemon died"; '
        f'while [ ! -e "{control["end"]}" ]; do sleep 0.05; done; '
        f'exit "$(cat "{control["end"]}")"'
    ))
    lmer = tmp_path / "lmer"
    lmer.write_text(LMER)
    lmer.chmod(0o755)
    return SimpleNamespace(root=root, lmer=lmer)


@pytest.fixture
def config(platform_root):
    return cfg.load({"lmer_bin": str(platform_root.lmer)})


def wait_for(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def spawn_then_kill_daemon(platform_root):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    completed = subprocess.run(
        [sys.executable, "-c", DAEMON, str(platform_root.root), str(platform_root.lmer)],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert completed.returncode == 0, completed.stderr
    spawned = json.loads(completed.stdout.strip().splitlines()[-1])
    assert wait_for(lambda: any(c.running for c in containers.discover())), (
        "the session never started its container"
    )
    return spawned


def log_text(session_id):
    try:
        return spawn.log_path_for(session_id).read_text(errors="replace")
    except OSError:
        return ""


def exit_events(session_id):
    return [
        e for e in store.read_events()
        if e["type"] == "session_exited" and e["data"]["session"] == session_id
    ]


def test_a_session_outlives_its_daemon_and_is_followed_again(
    platform_root, config, control
):
    session_id = spawn_then_kill_daemon(platform_root)["session"]
    [found] = containers.discover()
    assert found.running and found.session_id == session_id

    # Output after the daemon's death: the container is detached, so nothing
    # can turn it into a removal.
    control["go"].touch()
    time.sleep(0.5)
    assert containers.discover()[0].running

    [report] = reattach.reattach_all(config=config)
    assert report.output == reattach.OUTPUT_CONTAINER and not report.detached
    entry = registry.read_session(session_id)
    assert entry["container_id"] == found.id and not entry.get("detached")
    assert entry["container_name"] == found.name
    assert found.name.startswith("lmer-develop-")
    assert found.name.endswith("-" + session_id.rsplit("-", 1)[1])
    assert wait_for(lambda: "printed after the daemon died" in log_text(session_id)), (
        "the output nobody was attached for should be replayed into the log"
    )

    end_with(control, 0)
    assert spawn.wait_for_exit_recorded(session_id, timeout=20)
    assert [e["data"]["exit_code"] for e in exit_events(session_id)] == [0]
    assert registry.read_session(session_id) is None
    assert "container exited with code 0" in log_text(session_id)
    assert containers.discover() == [], "the follower removes the container"


def test_the_entry_learns_its_containers_id_and_name_once_it_is_created(
    platform_root, control
):
    """Recorded by the launching lmer at create, folded in on the read path."""
    session_id = spawn_then_kill_daemon(platform_root)["session"]
    [found] = containers.discover()
    assert registry.read_session(session_id)["container_name"] is None

    spawn.absorb_ports(registry.list_sessions(live_only=False))

    entry = registry.read_session(session_id)
    assert (entry["container_id"], entry["container_name"]) == (found.id, found.name)
    assert found.name.startswith("lmer-")


def test_a_container_that_ended_while_no_daemon_was_up_keeps_its_code(
    platform_root, config, control
):
    session_id = spawn_then_kill_daemon(platform_root)["session"]
    end_with(control, 5)
    control["go"].touch()
    # Exited, or already removed by the old follower if it outlived its terminal
    # (SIGHUP ignored): the code survives either way.
    assert wait_for(lambda: not any(c.running for c in containers.discover()))

    [report] = reattach.reattach_all(config=config)
    assert report.ended and report.output == reattach.OUTPUT_CONTAINER
    assert registry.read_session(session_id)["exited"]["code"] == 5
    assert [e["data"]["exit_code"] for e in exit_events(session_id)] == [5]
    assert "exited with code 5" in log_text(session_id)
    assert containers.discover() == []


def test_a_followed_session_can_still_be_exited(platform_root, config, control):
    session_id = spawn_then_kill_daemon(platform_root)["session"]
    reattach.reattach_all(config=config)

    assert lifecycle.exit_session(session_id).signals
    assert spawn.wait_for_exit_recorded(session_id, timeout=20)
    # The stop runs in the background, with the container's stop timeout.
    assert wait_for(lambda: containers.discover() == []), (
        "the exit stopped and removed the container"
    )


def test_a_container_coming_or_going_during_a_listing_is_not_an_error(
    platform_root, control, tmp_path
):
    """#589: a box with no config is one being created or mid-removal, and
    discovery polled across an exit's removal must not fail on it."""
    session_id = spawn_then_kill_daemon(platform_root)["session"]
    (tmp_path / "containers" / ("0" * 32)).mkdir()

    [found] = containers.discover()
    assert found.session_id == session_id


def test_a_cli_container_is_never_touched(platform_root, config, tmp_path):
    cid_file = tmp_path / "cli-cid"
    labels = runtime.container_labels(
        repo_host=None, repo_project=None, run_slug=None, platform_session=None,
    )
    subprocess.run(["docker", "create", *labels, "--cidfile", str(cid_file),
                    "fake-image", "/bin/sh", "-c", "exit 0"], check=True)
    subprocess.run(["docker", "start", cid_file.read_text()], check=True)

    assert containers.discover() == []
    assert reattach.reattach_all(config=config) == []
    assert (tmp_path / "containers" / cid_file.read_text()).exists()


def test_the_startup_notice_names_followed_and_ended_sessions():
    notice = reattach.startup_notice([
        reattach.ReattachReport("s-up", detached=False, draining=False,
                                output=reattach.OUTPUT_CONTAINER),
        reattach.ReattachReport("s-done", detached=False, draining=False,
                                output=reattach.OUTPUT_CONTAINER, ended=True,
                                detail="exit code 5"),
    ])
    assert "kept running in their containers and are followed again: s-up" in notice
    assert "ended while the daemon was down: s-done (exit code 5)" in notice


def test_a_container_not_yet_started_is_neither_followed_nor_recorded(
    platform_root, config, tmp_path
):
    """"Not running, code 0" is not an exit. Its lmer may be between create and
    start; with no live session behind it, it is litter holding the session's
    environment, and goes."""
    cid_file = tmp_path / "created-cid"
    labels = runtime.container_labels(
        repo_host=None, repo_project=None, run_slug=None, platform_session="s-new",
        platform_instance=containers.platform_instance(),
    )
    subprocess.run(["docker", "create", *labels, "--cidfile", str(cid_file),
                    "fake-image", "/bin/sh", "-c", "exit 0"], check=True)
    [found] = containers.discover()
    assert not found.started and found.exit_code is None
    assert reattach.reattach_all(config=config) == []
    assert containers.discover() == []


def test_a_stale_follower_is_ended_only_when_it_is_provably_the_same_process():
    sleeper = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        entry = {"id": "s-x", "pid": sleeper.pid, "pid_identity": "another-boot:1"}
        assert not spawn.end_stale_follower(entry), "a reused pid must be left alone"
        assert sleeper.poll() is None
        assert not spawn.end_stale_follower({**entry, "pid_identity": None})

        entry["pid_identity"] = spawn.pid_identity(sleeper.pid)
        assert spawn.end_stale_follower(entry)
        assert sleeper.wait(timeout=10) == -9
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait()


def test_two_platforms_on_one_runtime_keep_to_their_own(platform_root, tmp_path, monkeypatch):
    cid_file = tmp_path / "other-cid"
    labels = runtime.container_labels(
        repo_host=None, repo_project=None, run_slug=None, platform_session="s-other",
        platform_instance="someone-elses-platform",
    )
    subprocess.run(["docker", "create", *labels, "--cidfile", str(cid_file),
                    "fake-image", "/bin/sh", "-c", "exit 3"], check=True)
    subprocess.run(["docker", "start", cid_file.read_text()], check=True)
    time.sleep(0.5)
    assert containers.discover() == []
    containers.stop_for_session("s-other")
    assert (tmp_path / "containers" / cid_file.read_text()).exists()


def _labelled(tmp_path, session_id, script, *, instance=None):
    cid_file = tmp_path / f"cid-{session_id}"
    labels = runtime.container_labels(
        repo_host=None, repo_project=None, run_slug=None, platform_session=session_id,
        platform_instance=instance or containers.platform_instance(),
    )
    subprocess.run(["docker", "create", *labels, "--cidfile", str(cid_file),
                    "fake-image", "/bin/sh", "-c", script], check=True)
    return cid_file.read_text()


def test_an_untracked_running_container_is_stopped(platform_root, config, tmp_path):
    """Its entry went (an exit whose stop failed, say); nothing will follow it."""
    cid = _labelled(tmp_path, "s-gone", "trap 'exit 0' TERM; while true; do sleep 0.1; done")
    subprocess.run(["docker", "start", cid], check=True)
    assert reattach.reattach_all(config=config) == []
    assert wait_for(lambda: containers.discover() == [])


def test_the_output_of_a_container_that_exited_unwatched_reaches_the_log(
    platform_root, config, tmp_path
):
    """For a nonzero exit, that output is the diagnostic."""
    registry.register("s-quiet", pid=2**22, log_path=str(spawn.log_path_for("s-quiet")))
    log = spawn.log_path_for("s-quiet")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("before the restart\n")
    os.utime(log, (time.time() - 120, time.time() - 120))
    cid = _labelled(tmp_path, "s-quiet", "echo the reason it failed; exit 3")
    subprocess.run(["docker", "start", cid], check=True)
    assert wait_for(lambda: not any(c.running for c in containers.discover()))

    [report] = reattach.reattach_all(config=config)
    assert report.ended
    text = log.read_text()
    assert text.index("the reason it failed") < text.index("exited with code 3")
    assert registry.read_session("s-quiet")["exited"]["code"] == 3


def test_a_record_without_our_labels_is_never_ours():
    record = {"Id": "c1", "State": {"Status": "exited", "ExitCode": 0},
              "Config": {"Labels": {"io.lmer.managed": "1", "io.lmer.launcher": "platform",
                                    "io.lmer.platform.session": "s-1",
                                    "io.lmer.platform.instance": "not-this-one"}}}
    assert containers._parse(record) is None


def _record(status, *, running, code=0):
    return {"Id": "c1", "State": {"Status": status, "Running": running, "ExitCode": code},
            "Config": {"Labels": {"io.lmer.managed": "1", "io.lmer.launcher": "platform",
                                  "io.lmer.platform.session": "s-1",
                                  "io.lmer.platform.instance": containers.platform_instance()}}}


@pytest.mark.parametrize("reported", ["/lmer-chat-mr-389-ff2b", "lmer-chat-mr-389-ff2b"],
                         ids=["docker", "podman"])
def test_the_name_is_read_without_dockers_slash(platform_root, reported):
    record = {**_record("running", running=True), "Name": reported}
    assert containers._parse(record).name == "lmer-chat-mr-389-ff2b"
    assert containers._parse(_record("running", running=True)).name is None


def test_only_exited_and_dead_are_endings(platform_root):
    """podman reports a stop in flight as `stopping`, not running, code 0."""
    stopping = containers._parse(_record("stopping", running=False))
    assert stopping.running and stopping.exit_code is None
    for status in ("exited", "dead"):
        ended = containers._parse(_record(status, running=False, code=3))
        assert not ended.running and ended.exit_code == 3


def test_a_vanished_container_is_gone_and_an_unreadable_one_is_not(
    platform_root, tmp_path, monkeypatch
):
    cid = _labelled(tmp_path, "s-1", "sleep 30")
    subprocess.run(["docker", "start", cid], check=True)
    monkeypatch.setenv("FAKE_RUNTIME_FAIL_INSPECT", "1")
    with pytest.raises(containers.DiscoveryError):
        containers.inspect_one(cid)  # still listed: unknown, not gone
    assert containers.discover() == []  # skipped, and logged
    monkeypatch.delenv("FAKE_RUNTIME_FAIL_INSPECT")
    containers.remove(cid)
    assert containers.inspect_one(cid) is None


def test_a_failed_reinspect_after_ending_the_old_follower_still_follows(
    platform_root, config, monkeypatch
):
    registry.register("s-1", pid=os.getpid(), log_path=str(spawn.log_path_for("s-1")))
    followed = []
    monkeypatch.setattr(reattach, "end_stale_follower", lambda entry: True)

    def unreadable(container_id):
        raise containers.DiscoveryError("runtime timed out")

    monkeypatch.setattr(containers, "inspect_one", unreadable)
    monkeypatch.setattr(reattach, "follow_session",
                        lambda config, session, container, since=None, container_name=None:
                        followed.append(container))
    found = containers.SessionContainer(id="c1", session_id="s-1", running=True, exit_code=None)
    report = reattach._adopt_container(config, found)
    assert followed == ["c1"] and report.output == reattach.OUTPUT_CONTAINER
