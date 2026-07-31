"""Tests for ending a session on purpose (issue #141, slice M3 / T27).

Two verbs that both "stop" a session and must never be interchangeable, so almost
every test here is about the *difference* between them:

- **wind down asks and waits.** It is a prompt over the control plane, so it signals
  nothing — pinned against a real, live process that has to still be there
  afterwards — and it reaches a session whose host terminal died with a previous
  daemon, because HTTP into the container does not care about the process table.
- **exit signals, now.** Pinned against a real session *with a child process*,
  because that is the whole reason the signal goes to the process group: ``lmer``
  is not the thing holding the container, ``podman run`` is, and it is a child.
  Every session the platform does not own is refused rather than signalled, which
  is the one failure mode here that would be unrecoverable — the pid on an old
  entry may since have been reused by something entirely unrelated.

Sessions are spawned for real against a stub standing in for ``lmer`` (the
test_platform_spawn.py approach) wherever a process matters, and no test leaves one
behind. The control plane is the real loopback HTTP double from
tests/test_platform_session_io.py rather than a patched function: the interesting
half of a wind-down is that a specific paragraph arrives at ``/input`` with the
session's own bearer token, and stubbing the transport is exactly what would stop
testing that.

The UI half is source-level, like tests/test_platform_web_shell.py: there is no JS
runner in this image, and "the two verbs got equal prominence" is a change that
renders perfectly and quietly destroys the property the feature exists for.
"""

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lmer_platform import api, lifecycle, reattach, registry, spawn, store
from lmer_platform import config as cfg
from lmer_platform.session_io import ControlUnavailable, SessionIOError
from tests.conftest import strip_lmer_env
# The control-plane double and its token, borrowed rather than copied: it is the
# stand-in for one session's supervisor and there is no reason for two of them to
# drift. It belongs in a shared tests/ helper; that file was outside this slice.
from tests.test_platform_session_io import CONTROL_TOKEN, FakeControlPlane

SECRET = "test-secret-value"
WEB = Path(__file__).resolve().parent.parent / "web"
RUN_DETAIL = WEB / "src" / "components" / "RunDetail.vue"
API_CLIENT = WEB / "src" / "api.js"

#: A pid nothing can be running under, so an entry reads as crashed. Same value the
#: rest of the platform tests use.
DEAD_PID = 2**22


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in ("LMER_REPO_URL", "LMER_PLATFORM_PORTS_FILE", "LMER_TASK",
                 "LMER_TASK_TARGET"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture
def fake_lmer(tmp_path):
    """A stub standing in for `lmer`, which can also leave a child behind.

    The child is the point of the group-signal tests: the host-side ``lmer`` is not
    what holds the container — it runs ``podman run`` as its own child — so a stub
    without one would let a signal aimed at the pid alone look like a working exit.
    ``sh -c`` rather than a subshell, because ``$$`` inside ``( )`` is still the
    parent shell's pid; a fresh ``sh`` that ``exec``s reports the pid the sleep
    actually runs under.

    The child **ignores SIGHUP**, and without that this stub cannot tell a group
    signal from a pid signal at all. The kernel sends SIGHUP to the foreground
    process group when a session leader dies, so killing the stub alone appears to
    take its children with it — until a child has ignored SIGHUP (which survives
    ``exec``), or left the foreground group, or made a session of its own. Those are
    the children that outlive a pid-only signal, and one of them holds a container.
    """
    script = tmp_path / "fake-lmer"
    script.write_text(
        "#!/bin/sh\n"
        'echo "fake lmer started: $*"\n'
        'if [ -n "$FAKE_LMER_CHILD_FILE" ]; then\n'
        "  sh -c 'trap \"\" HUP; echo $$ > \"$FAKE_LMER_CHILD_FILE\"; "
        "exec sleep 300' &\n"
        "fi\n"
        # Staying alive is how a test keeps the registry entry around: a clean exit
        # reaps it, which would otherwise race any assertion about it.
        'if [ -n "$FAKE_LMER_SLEEP" ]; then sleep "$FAKE_LMER_SLEEP"; fi\n'
        'exit "${FAKE_LMER_EXIT:-0}"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


@pytest.fixture
def stubborn_lmer(tmp_path):
    """A stub that ignores SIGTERM, for the escalation test.

    Python rather than a shell script, and one process rather than a group: a shell
    that traps TERM still ends when the ``sleep`` it is waiting on is killed by the
    same group signal, so the escalation would appear to work while never happening.
    Nothing survives SIGKILL, which is the point of having it in the ladder.
    """
    script = tmp_path / "stubborn-lmer"
    script.write_text(
        f"#!{sys.executable}\n"
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('stubborn lmer started', flush=True)\n"
        "time.sleep(300)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


@pytest.fixture
def config(platform_root, fake_lmer):
    return cfg.load({"lmer_bin": str(fake_lmer)})


@pytest.fixture
def control_plane():
    plane = FakeControlPlane()
    yield plane
    plane.stop()


@pytest.fixture
def client(config):
    """A client over the real routes, with a stub fleet view (no work repo)."""
    app = api.create_app(
        config, SECRET, state_builder=lambda config, force_pull=False: {}
    )
    return TestClient(app)


def bearer_header(token=SECRET):
    return {"Authorization": f"Bearer {token}"}


def request_for(**overrides):
    payload = {
        "taskdef": "develop",
        "target": "https://gitlab.example.com/agents/global/-/work_items/141",
        "repo_url": "https://gitlab.example.com/agents/global.git",
    }
    payload.update(overrides)
    return spawn.SpawnRequest(**payload)


def wait_for(predicate, timeout=10.0):
    """Poll until *predicate* holds — spawning and dying are both asynchronous."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def alive(pid):
    return registry.is_live({"pid": pid})


def wait_for_output(result, text, timeout=10.0):
    """Wait until the session's log carries *text*.

    A real synchronisation point rather than a sleep, and one test genuinely needs
    it: signalling a stub during its interpreter's startup kills it before the line
    that installs its signal handler has run, and the escalation being tested then
    never happens.
    """
    needle = text.encode("utf-8")

    def printed():
        try:
            return needle in result.log_path.read_bytes()
        except OSError:
            return False

    return wait_for(printed, timeout=timeout)


def reap(pid):
    """Make sure no test leaves a live session behind, however it ended."""
    with contextlib.suppress(OSError):
        os.killpg(pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)


def plant_session(session_id, *, port=None, live=True, credential=CONTROL_TOKEN):
    """Register a session, optionally with a control plane. No process is started.

    For the paths where the *process* is irrelevant — wind down never signals — this
    keeps the test to the thing being tested. Every exit test spawns for real
    instead, because there the process is the subject.
    """
    control = {}
    if port is not None:
        control = {
            "host": "127.0.0.1",
            "port": port,
            "token_ref": str(spawn.token_file_for(session_id)),
        }
    registry.register(
        session_id, pid=os.getpid() if live else DEAD_PID, control=control
    )
    if port is not None and credential is not None:
        spawn.token_file_for(session_id).write_text(credential, encoding="utf-8")


def forge_owner_pid(session_id, owner_pid):
    """Rewrite an entry's ``owner_pid`` behind the registry's back.

    ``registry.update`` refreshes that field to the current writer by design, so a
    test about *not trusting* it has to write the file the way a person with an
    editor would.
    """
    path = registry.session_path(session_id)
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["owner_pid"] = owner_pid
    path.write_text(json.dumps(entry), encoding="utf-8")


def redirect_control(session_id, port):
    """Point a really-spawned session's control plane at the test double.

    The session was spawned for real, so it has a real control port that nothing is
    listening on (there is no container). Only the port moves: the token stays the
    one the spawn minted on disk, which is what the assertions about the bearer
    header are made of.
    """
    entry = registry.read_session(session_id)
    registry.update(session_id, control={**entry["control"], "port": port})


def live_session(config, *, sleep="30", child_file=None, kind="worker", **env):
    """Spawn a real session that stays up, and hand back its result."""
    os.environ["FAKE_LMER_SLEEP"] = sleep
    if child_file is not None:
        os.environ["FAKE_LMER_CHILD_FILE"] = str(child_file)
    for name, value in env.items():
        os.environ[name] = value
    try:
        return spawn.spawn_session(config, request_for(), kind=kind)
    finally:
        os.environ.pop("FAKE_LMER_SLEEP", None)
        os.environ.pop("FAKE_LMER_CHILD_FILE", None)
        for name in env:
            os.environ.pop(name, None)


def snapshot_tree(root):
    """Every file under *root* as ``{relative path: bytes}``.

    The D3 guard's instrument, borrowed from tests/test_platform_answer.py: the
    platform must not write run state, and the mirror is the only run state it can
    reach. Contents rather than mtimes, because a rewrite with identical bytes is
    still a write the platform must not make.
    """
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def plant_mirror_run(config):
    """A run dir in the mirror, written as bytes so any write to it is visible."""
    path = config.mirror_path / "gitlab.example.com" / "agents/global" / "runs" / "r-1"
    path.mkdir(parents=True, exist_ok=True)
    (path / "state.yaml").write_text(
        "schema: 1\nstatus: in-progress\n", encoding="utf-8"
    )
    return config.mirror_path


def lifecycle_events(types=None):
    """Platform events, optionally filtered to the types being asserted about."""
    events = store.read_events()
    if types is None:
        return events
    return [event for event in events if event.get("type") in types]


# --- the wind-down prompt ----------------------------------------------------

def test_the_prompt_says_what_wrapping_up_means():
    """A wind-down that does not say what to do is a session that stops mid-thought.

    Not pinning prose for its own sake: each of these is a thing the agent has to be
    told or the verb loses its whole advantage over exit — that the work lands.
    """
    prompt = lifecycle.wind_down_prompt()

    assert prompt.startswith(lifecycle.PLATFORM_PREFIX), (
        "the scrollback has to show who said this; an unattributed instruction "
        "reads as the operator typing"
    )
    assert "not a new task" in prompt, (
        "an agent mid-turn reads incoming text as work, and a wind-down that "
        "starts a fresh investigation is worse than no wind-down"
    )
    for expected in ("commit and push", "record the run's state", "summary"):
        assert expected in prompt, f"the prompt never mentions {expected!r}"
    assert "end the session" in prompt, (
        "wind down is the agent terminating itself; a prompt that only says "
        "'wrap up' leaves the container running forever"
    )
    # Originally this read "end the session yourself, however your instructions
    # say a session ends" — deliberately naming no command, because none existed
    # that a non-chat taskdef could use. An operator decision ("decouple it from
    # slack, lmer should be able to shut down its session") produced one, so the
    # prompt names it: an instruction the agent cannot act on is a button that does
    # nothing. See tests/test_session_end.py for the verb itself.
    # Both spellings, because a console script is generated at install time: an
    # image built before lmer-end-session existed does not have it on PATH, and the
    # agent was told to run a command it could not find. src/ is mounted, so the
    # module form always works — naming only the script is the wrong side of the
    # gap between "built" and "usable".
    assert "python -m lmer_cli.session_end" in prompt, (
        "the prompt names only the console script, which an older image lacks"
    )
    assert "lmer-end-session" in prompt, (
        "the prompt must name the command that ends a session — 'however your "
        "instructions say a session ends' is nothing at all for a develop run"
    )
    assert "kill this container" in prompt, (
        "the agent has to know it is not racing a timer, or it will cut corners"
    )
    assert "has not seen yet" in prompt, (
        "D22: a session may be holding a port or a page nobody has looked at, and "
        "being asked to stop is not the same as that having been seen"
    )


@pytest.mark.parametrize("note", [None, "and skip the MR", "multi\nline\nnote"])
def test_the_prompt_is_one_line_whatever_is_added_to_it(note):
    """A newline in this payload is a truncated instruction, not a formatting nit.

    It lands in a PTY in raw mode. claude's TUI treats a bare LF as a literal
    newline in the input box, but a harness that submits on the first one would
    deliver the agent "you have been asked to wind down" and nothing about what
    that means — and the platform would report the request as delivered.
    """
    prompt = lifecycle.wind_down_prompt(note)

    assert "\n" not in prompt
    assert "\r" not in prompt


def test_an_operator_note_rides_on_the_end_of_the_prompt():
    prompt = lifecycle.wind_down_prompt("skip the MR, just push the branch")

    assert prompt.startswith(lifecycle.WIND_DOWN_PROMPT), (
        "the operator's addition must not displace the instruction"
    )
    assert prompt.endswith("skip the MR, just push the branch")


def test_a_multiline_note_is_collapsed_rather_than_refused():
    """A textarea is where a newline comes from, and refusing one is unhelpful."""
    prompt = lifecycle.wind_down_prompt("first line\n\nsecond   line\t")

    assert prompt.endswith("first line second line")


@pytest.mark.parametrize("note", ["", "   ", "\n\n"])
def test_a_blank_note_changes_nothing(note):
    assert lifecycle.wind_down_prompt(note) == lifecycle.WIND_DOWN_PROMPT


def test_an_oversized_note_is_refused():
    with pytest.raises(lifecycle.LifecycleError, match="limit"):
        lifecycle.wind_down_prompt("x" * (lifecycle.MAX_NOTE_CHARS + 1))


def test_a_note_that_is_not_text_is_refused():
    with pytest.raises(lifecycle.LifecycleError, match="must be text"):
        lifecycle.wind_down_prompt({"note": "nice try"})


# --- winding down -----------------------------------------------------------

def test_wind_down_types_the_prompt_into_the_session(platform_root, control_plane):
    plant_session("s-1", port=control_plane.port)

    report = lifecycle.wind_down("s-1")

    assert control_plane.calls == [{
        "path": "/input",
        "authorization": f"Bearer {CONTROL_TOKEN}",
        "body": {"data": lifecycle.WIND_DOWN_PROMPT, "append_newline": True},
    }]
    assert report.prompt == lifecycle.WIND_DOWN_PROMPT


def test_wind_down_presses_enter(platform_root, control_plane):
    """Without it the paragraph sits unsent in the agent's input box forever."""
    plant_session("s-1", port=control_plane.port)
    lifecycle.wind_down("s-1")

    assert control_plane.calls[0]["body"]["append_newline"] is True


def test_an_invalid_note_is_refused_before_anything_is_sent(
    platform_root, control_plane
):
    """The prompt is built first, so a note the platform will not carry costs the
    operator an error and not a half-delivered wind-down."""
    plant_session("s-1", port=control_plane.port)

    with pytest.raises(lifecycle.LifecycleError):
        lifecycle.wind_down("s-1", note="x" * (lifecycle.MAX_NOTE_CHARS + 1))

    assert control_plane.calls == []
    assert "lifecycle" not in registry.read_session("s-1")


def test_wind_down_records_the_request_on_the_entry(platform_root, control_plane):
    """The record is what lets the UI say "asked four minutes ago, still going"."""
    plant_session("s-1", port=control_plane.port)

    report = lifecycle.wind_down("s-1", note="skip the MR")

    record = registry.read_session("s-1")["lifecycle"]
    assert record["verb"] == lifecycle.VERB_WIND_DOWN
    assert record["requested_at"] == report.requested_at
    assert record["backstop_at"] == report.backstop_at > report.requested_at
    assert record["note"] == "skip the MR"
    assert report.recorded is True


def test_the_recorded_backstop_is_a_deadline_and_not_a_timer(
    platform_root, control_plane
):
    """Spec R18/D22: past the deadline something *says so*; nothing escalates.

    The absence being pinned is a kill: a wind-down that quietly turned into a
    signal would be the one behaviour D22 forbids outright.
    """
    plant_session("s-1", port=control_plane.port)
    report = lifecycle.wind_down("s-1")

    assert report.backstop_at
    assert registry.read_session("s-1") is not None, (
        "nothing about a wind-down may remove the session"
    )
    assert lifecycle_events({"session_exit_requested"}) == []


def test_wind_down_signals_nothing_at_all(config, platform_root, control_plane):
    """The load-bearing property, against a real process: it is still there after.

    A wind-down that killed anything would be an exit with better manners, and the
    session an operator was told would wrap up would lose whatever it had not
    committed.
    """
    result = live_session(config)
    try:
        redirect_control(result.session_id, control_plane.port)

        lifecycle.wind_down(result.session_id)

        assert alive(result.pid), "wind down must not touch the process"
        assert registry.read_session(result.session_id) is not None
        # And after the grace an exit would have waited out, still there.
        time.sleep(lifecycle.EXIT_KILL_GRACE_SECONDS)
        assert alive(result.pid)
    finally:
        reap(result.pid)


def test_wind_down_reaches_a_session_that_survived_a_daemon_restart(
    platform_root, control_plane
):
    """T36: the host PTY died with the last daemon, so this is the verb that works.

    It travels the control plane — an HTTP call to a process inside the container —
    which is precisely the half of the platform a daemon restart does not touch.
    Exit cannot reach this session at all (see below), and that asymmetry is why
    wind down is the default rather than the polite option.
    """
    plant_session("s-1", port=control_plane.port)
    reattach.mark_detached(
        "s-1",
        output=reattach.OUTPUT_CONTROL_PLANE,
        detail="its host terminal is gone; output recovered over the control plane",
        cursor=17,
    )

    report = lifecycle.wind_down("s-1")

    assert report.recorded is True
    assert control_plane.calls[0]["body"]["data"] == lifecycle.WIND_DOWN_PROMPT


def test_wind_down_on_a_session_with_no_control_plane_is_refused(platform_root):
    plant_session("s-1")

    with pytest.raises(ControlUnavailable) as caught:
        lifecycle.wind_down("s-1")
    assert caught.value.status == 409


def test_wind_down_on_a_dead_session_is_refused(platform_root, control_plane):
    plant_session("s-dead", port=control_plane.port, live=False)

    with pytest.raises(ControlUnavailable):
        lifecycle.wind_down("s-dead")
    assert control_plane.calls == [], "a dead session must not be dialled"


def test_wind_down_on_an_unknown_session_is_refused(platform_root):
    with pytest.raises(SessionIOError) as caught:
        lifecycle.wind_down("s-never-existed")
    assert caught.value.status == 404


def test_a_refused_prompt_is_never_reported_as_a_wind_down(
    platform_root, control_plane
):
    """An operator told the session is wrapping up, while it never heard, is worse
    than an error: they come back hours later to a session still working."""
    control_plane.answer("/input", 500, {"detail": "no pty"})
    plant_session("s-1", port=control_plane.port)

    with pytest.raises(SessionIOError):
        lifecycle.wind_down("s-1")

    assert "lifecycle" not in (registry.read_session("s-1") or {})
    assert lifecycle_events({"session_wind_down_requested"}) == []


def test_an_unrecordable_wind_down_still_reports_the_request(
    platform_root, control_plane, monkeypatch, caplog
):
    """The prompt is delivered before the mark, so a failed mark loses the mark.

    Reported in the return value rather than raised: the agent has already been
    asked, and an exception here would send the operator to do it again.
    """
    plant_session("s-1", port=control_plane.port)

    def unwritable(*_args, **_kwargs):
        raise store.StoreError("state dir is read-only")

    monkeypatch.setattr(registry, "update", unwritable)

    report = lifecycle.wind_down("s-1")

    assert report.recorded is False
    assert control_plane.calls, "the prompt still went"
    assert any(
        "platform_wind_down_unrecorded" in record.message for record in caplog.records
    )


def test_a_session_that_exits_before_the_mark_is_not_an_error(
    platform_root, control_plane, monkeypatch
):
    """The narrow race, staged where it actually happens: the prompt lands, and the
    agent acts on it and is gone before the platform gets to write the mark."""
    plant_session("s-1", port=control_plane.port)
    real_send = lifecycle.send_input

    def send_then_vanish(*args, **kwargs):
        reply = real_send(*args, **kwargs)
        registry.remove("s-1")
        return reply

    monkeypatch.setattr(lifecycle, "send_input", send_then_vanish)

    report = lifecycle.wind_down("s-1")

    assert report.recorded is False
    assert control_plane.calls, "the prompt still went"


def test_wind_down_writes_nothing_to_run_state(config, platform_root, control_plane):
    """Spec D3: the platform never writes run state — that is the agent's record,
    and giving it the chance to write it is the entire point of this verb."""
    mirror = plant_mirror_run(config)
    before = snapshot_tree(mirror)
    plant_session("s-1", port=control_plane.port)

    lifecycle.wind_down("s-1", note="and push")

    assert snapshot_tree(mirror) == before


def test_the_note_is_recorded_but_the_prompt_boilerplate_is_not(
    platform_root, control_plane
):
    """Copying the constant into every entry would make a wording change look like
    a state migration."""
    plant_session("s-1", port=control_plane.port)
    lifecycle.wind_down("s-1", note="skip the MR")

    record = registry.read_session("s-1")["lifecycle"]
    assert lifecycle.WIND_DOWN_PROMPT not in json.dumps(record)
    assert record["note"] == "skip the MR"


# --- exiting ----------------------------------------------------------------

def test_exit_ends_the_session_and_the_children_holding_the_container(
    config, platform_root, tmp_path
):
    """Why the signal goes to the process group, pinned with a real child process.

    ``lmer`` does not hold the container — it runs ``podman run`` as its own child
    and installs no handler of its own — so a signal delivered to the session's pid
    alone kills the bookkeeping and leaves a container running with nothing watching
    it. The stub's ``sleep`` child stands in for that, and it has to die too.

    The child ignores SIGHUP on purpose; see the fixture. Without that this test
    passes either way, because the kernel HUPs the foreground group when a session
    leader dies and does the group kill for you — for every child that has not
    opted out of it. That masking is what made the first version of this test
    useless, and a mutation run is what found it out.
    """
    child_file = tmp_path / "child.pid"
    result = live_session(config, child_file=child_file)
    child = None
    try:
        assert wait_for(lambda: child_file.is_file() and child_file.read_text().strip())
        child = int(child_file.read_text().strip())
        assert alive(child), "the stub never got its child up"

        report = lifecycle.exit_session(result.session_id)

        assert report.signals, "nothing was signalled at all"
        assert not alive(result.pid)
        assert wait_for(lambda: not alive(child)), (
            "the session's child outlived the exit — the signal went to the pid "
            "and not to the process group, which is how a container survives an "
            "exit with nothing left watching it"
        )
    finally:
        reap(result.pid)
        if child:
            reap(child)


def test_exit_takes_sigterm_first(config, platform_root):
    """SIGTERM is the signal that gets the container torn down; SIGKILL only
    guarantees the session ends and can leave the container behind."""
    result = live_session(config)
    try:
        report = lifecycle.exit_session(result.session_id)

        assert report.signals == ("SIGTERM",)
        assert report.pid == result.pid
    finally:
        reap(result.pid)


def test_exit_escalates_to_sigkill_when_sigterm_is_ignored(
    platform_root, stubborn_lmer, monkeypatch
):
    monkeypatch.setattr(lifecycle, "EXIT_GRACE_SECONDS", 0.3)
    config = cfg.load({"lmer_bin": str(stubborn_lmer)})
    result = spawn.spawn_session(config, request_for())
    try:
        # Not just "alive": a SIGTERM that arrives while the interpreter is still
        # starting kills it before the line that ignores SIGTERM has run, and the
        # escalation this test is about never happens.
        assert wait_for_output(result, "stubborn lmer started")

        report = lifecycle.exit_session(result.session_id)

        assert report.signals == ("SIGTERM", "SIGKILL")
        assert not alive(result.pid)
    finally:
        reap(result.pid)


def test_exit_removes_the_entry_so_a_requested_ending_is_not_a_crash(
    config, platform_root
):
    """A signalled process never exits 0, so the watcher keeps its entry as the
    crash signal — and a session we killed on request must not read as one that
    died in the fleet view."""
    result = live_session(config)
    try:
        report = lifecycle.exit_session(result.session_id)

        assert report.entry_removed is True
        assert registry.read_session(result.session_id) is None
    finally:
        reap(result.pid)


def test_exit_keeps_the_scrollback(config, platform_root):
    """Spec D16: the log outlives the container, and it is the only record left of
    everything the session did."""
    result = live_session(config)
    try:
        assert wait_for_output(result, "fake lmer started")
        before = result.log_path.read_bytes()

        lifecycle.exit_session(result.session_id)

        assert result.log_path.is_file()
        assert before in result.log_path.read_bytes()
    finally:
        reap(result.pid)


def test_exit_records_the_request_before_it_signals(config, platform_root):
    """A daemon that dies mid-ladder still leaves the evidence that this ending was
    asked for rather than suffered."""
    result = live_session(config)
    try:
        lifecycle.exit_session(result.session_id)

        requested = lifecycle_events({"session_exit_requested"})
        assert [event["data"]["session"] for event in requested] == [result.session_id]
        assert requested[0]["data"]["pid"] == result.pid
    finally:
        reap(result.pid)


def test_exit_writes_nothing_to_run_state(config, platform_root):
    """Spec D3, and the sharper version of it: an exit that scribbled "terminated"
    into the mirror would be the platform asserting an ending the run never got to
    describe."""
    mirror = plant_mirror_run(config)
    before = snapshot_tree(mirror)
    result = live_session(config)
    try:
        lifecycle.exit_session(result.session_id)
        assert snapshot_tree(mirror) == before
    finally:
        reap(result.pid)


def test_a_reattached_session_is_never_signalled(config, platform_root):
    """T36's session is alive and readable but is no longer this platform's child.

    The refusal is not squeamishness. The daemon holds an un-reaped child slot for
    every session it spawned, which is what keeps that pid from being recycled; for
    a session it merely re-adopted the log of, the pid on the entry is a number that
    was true once. Signalling that is not a failed exit, it is killing a stranger.
    """
    result = live_session(config)
    try:
        reattach.mark_detached(
            result.session_id,
            output=reattach.OUTPUT_CONTROL_PLANE,
            detail="host terminal lost with the last daemon",
            cursor=0,
        )

        with pytest.raises(lifecycle.SessionNotTerminable) as caught:
            lifecycle.exit_session(result.session_id)

        assert caught.value.status == 409
        assert "wind" in str(caught.value).lower(), (
            "a refusal has to name the verb that does work here"
        )
        assert str(result.pid) in str(caught.value), (
            "the operator finishing this by hand needs the pid"
        )
        assert alive(result.pid), "the refusal must actually refuse"
        assert registry.read_session(result.session_id) is not None
    finally:
        reap(result.pid)


def test_a_session_started_by_a_process_that_is_gone_is_never_signalled(
    platform_root, tmp_path
):
    """The other orphan: ``lmer platform spawn`` spawns and exits.

    Its session's pid is reserved by nothing, exactly like the re-attached case, so
    a daemon asked to exit it would be signalling a pid that may since have been
    reused. Built with a real orphan — a process whose parent has exited — rather
    than by editing ``owner_pid``, because the check reads the kernel and not the
    entry.
    """
    marker = tmp_path / "orphan.pid"
    subprocess.run(
        ["sh", "-c", f'sleep 60 & echo $! > "{marker}"'], check=True, timeout=30
    )
    assert wait_for(lambda: marker.is_file() and marker.read_text().strip())
    orphan = int(marker.read_text().strip())
    try:
        assert wait_for(lambda: lifecycle._parent_pid(orphan) != os.getpid()), (
            "the sleep is still our child, so this test would prove nothing"
        )
        registry.register("s-orphan", pid=orphan)

        with pytest.raises(lifecycle.SessionNotTerminable) as caught:
            lifecycle.exit_session("s-orphan")

        assert caught.value.status == 409
        assert "wind" in str(caught.value).lower()
        assert alive(orphan)
    finally:
        reap(orphan)


def test_a_pid_that_could_not_name_a_session_is_never_signalled(platform_root):
    """1 is init. 0 and -1 mean "every process I can signal" to kill(2), and the
    registry refuses to write those at all — but a pid of 1 it will take."""
    registry.register("s-init", pid=1)

    with pytest.raises(lifecycle.SessionNotTerminable, match="no usable pid"):
        lifecycle.exit_session("s-init")


def test_ownership_is_read_from_the_kernel_and_not_from_the_entry(
    config, platform_root
):
    """``owner_pid`` is a field in a file an operator can edit; a signal deserves
    better than a claim, so the check asks the kernel who the parent is."""
    result = live_session(config)
    try:
        forge_owner_pid(result.session_id, DEAD_PID)

        # Still signallable: the entry's claim is wrong and irrelevant, because this
        # process really is the parent.
        assert lifecycle._parent_pid(result.pid) == os.getpid()
        assert lifecycle.exit_session(result.session_id).signals
    finally:
        reap(result.pid)


def test_without_proc_the_entrys_own_claim_is_the_fallback(
    config, platform_root, monkeypatch
):
    """macOS has no ``/proc``, and a verb that does not exist there is worse than
    one resting on a weaker check — but the weaker check still has to refuse."""
    result = live_session(config)
    try:
        monkeypatch.setattr(lifecycle, "_parent_pid", lambda pid: None)
        forge_owner_pid(result.session_id, DEAD_PID)

        with pytest.raises(lifecycle.SessionNotTerminable, match="another process"):
            lifecycle.exit_session(result.session_id)
        assert alive(result.pid)
    finally:
        reap(result.pid)


def test_without_proc_a_session_this_process_started_is_still_signallable(
    config, platform_root, monkeypatch
):
    """The other half of the fallback: it must not be a blanket refusal, or exit
    would simply not work on a host without ``/proc``."""
    result = live_session(config)
    try:
        monkeypatch.setattr(lifecycle, "_parent_pid", lambda pid: None)

        assert lifecycle.exit_session(result.session_id).signals == ("SIGTERM",)
        assert not alive(result.pid)
    finally:
        reap(result.pid)


def test_a_session_that_does_not_lead_a_group_is_signalled_alone(
    platform_root, monkeypatch, caplog
):
    """The other side of the group signal, and the reason leadership is checked.

    ``killpg`` on a pid that does not lead a group signals whatever group happens to
    carry that id — for a child started without a session of its own, that is the
    daemon's own group, so the "kill the container's holder too" convenience would
    take the platform down with the session. ``killpg`` is stubbed here rather than
    trusted: the assertion is that it is not called, and a regression would
    otherwise prove itself by killing the test run.
    """
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    group_signals = []
    monkeypatch.setattr(
        lifecycle.os, "killpg", lambda pid, sig: group_signals.append((pid, sig))
    )
    try:
        registry.register("s-1", pid=child.pid)
        assert os.getpgid(child.pid) != child.pid, "the child leads no group here"

        report = lifecycle.exit_session("s-1")

        assert group_signals == [], (
            "a non-leader's pid was passed to killpg, which would signal this "
            "process's own group"
        )
        assert report.signals == ("SIGTERM",)
        assert any(
            "platform_session_not_group_leader" in record.message
            for record in caplog.records
        )
    finally:
        child.kill()
        child.wait(timeout=10)


def test_the_daemons_own_pid_is_never_signalled(platform_root):
    """Reachable: the pid is read back out of a file an operator can edit, and this
    one would take the platform down with the session."""
    registry.register("s-self", pid=os.getpid())

    with pytest.raises(lifecycle.SessionNotTerminable, match="own pid"):
        lifecycle.exit_session("s-self")


def test_the_assistant_is_not_exited_through_the_generic_verb(config, platform_root):
    """Stopping the orchestrator also has to clear its pointer and stop reason.

    That bookkeeping lives in lmer_platform.assistant, and the fleet view lists the
    assistant as a row like any other — so this is one tap away in the UI, not a
    hypothetical.
    """
    result = live_session(config, kind="assistant")
    try:
        with pytest.raises(lifecycle.SessionNotTerminable) as caught:
            lifecycle.exit_session(result.session_id)

        assert "assistant" in str(caught.value)
        assert alive(result.pid)
    finally:
        reap(result.pid)


def test_exit_refuses_a_session_whose_process_is_already_gone(platform_root):
    plant_session("s-dead", live=False)

    with pytest.raises(lifecycle.SessionNotTerminable, match="already gone"):
        lifecycle.exit_session("s-dead")
    assert registry.read_session("s-dead") is not None, (
        "the stale entry is the crash signal; an exit must not quietly reap it"
    )


def test_exit_refuses_a_session_with_no_entry(platform_root):
    with pytest.raises(lifecycle.SessionNotTerminable, match="no registry entry"):
        lifecycle.exit_session("s-never-existed")


def test_a_session_that_survives_the_whole_ladder_keeps_its_entry(
    config, platform_root, monkeypatch
):
    """The EPERM shape: signals accepted, nothing dies.

    The process is real and stays real; only delivery is neutered, because there is
    no honest way to make a process ignore SIGKILL. What matters is the aftermath —
    the entry survives, so the fleet view goes on showing a session that is in fact
    still running, and the caller gets a 500 rather than a cheerful 200.
    """
    monkeypatch.setattr(lifecycle, "EXIT_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(lifecycle, "EXIT_KILL_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(lifecycle, "_signal_group", lambda pid, sig: True)
    result = live_session(config)
    try:
        with pytest.raises(lifecycle.TerminationFailed) as caught:
            lifecycle.exit_session(result.session_id)

        assert caught.value.status == 500
        assert "SIGTERM then SIGKILL" in str(caught.value)
        assert registry.read_session(result.session_id) is not None
        assert alive(result.pid)
        # Requested *then* failed: the first event is written before the first
        # signal, so a daemon that dies mid-ladder still leaves the evidence.
        assert [event["type"] for event in lifecycle_events(
            {"session_exit_requested", "session_exit_failed"}
        )] == ["session_exit_requested", "session_exit_failed"]
    finally:
        reap(result.pid)


def test_a_process_that_goes_between_the_check_and_the_signal_is_fine(
    config, platform_root, monkeypatch
):
    """A race, not an error: it was alive at the check and gone at the signal.

    Staged inside the ladder, which is the only place it can happen — an exit
    starts by refusing a session whose process has already gone, so the window is
    between that check and ``killpg`` raising ProcessLookupError.
    """
    result = live_session(config)

    def vanished(pid, sig):
        reap(pid)
        assert wait_for(lambda: not alive(pid))
        return False

    monkeypatch.setattr(lifecycle, "_signal_group", vanished)

    report = lifecycle.exit_session(result.session_id)

    assert report.signals == ()
    assert report.entry_removed is True


# --- the routes -------------------------------------------------------------

@pytest.mark.parametrize("verb", ["wind-down", "exit"])
def test_the_routes_require_the_shared_secret(client, verb):
    assert client.post(f"/api/sessions/s-1/{verb}").status_code == 401


def test_the_wind_down_route_answers_202_with_what_it_sent(
    client, platform_root, control_plane
):
    """202 because nothing has ended: an agent has been asked, and a 200 would tell
    an operator a container that is still holding a slot is over."""
    plant_session("s-1", port=control_plane.port)

    response = client.post(
        "/api/sessions/s-1/wind-down", headers=bearer_header(), json={"note": "push it"}
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["verb"] == lifecycle.VERB_WIND_DOWN
    assert payload["prompt"].endswith("push it"), (
        "the operator interrupted a working agent; what it was told should not be "
        "something only the daemon knows"
    )
    assert payload["backstop_at"] > payload["requested_at"]


def test_the_wind_down_route_takes_no_body_at_all(
    client, platform_root, control_plane
):
    """A bare POST from curl is how this gets used from a terminal."""
    plant_session("s-1", port=control_plane.port)

    response = client.post("/api/sessions/s-1/wind-down", headers=bearer_header())

    assert response.status_code == 202
    assert control_plane.calls[0]["body"]["data"] == lifecycle.WIND_DOWN_PROMPT


def test_the_wind_down_route_404s_for_an_unknown_session(client, platform_root):
    response = client.post(
        "/api/sessions/s-nope/wind-down", headers=bearer_header()
    )
    assert response.status_code == 404


def test_the_wind_down_route_never_leaks_the_session_token(
    client, platform_root, control_plane
):
    control_plane.answer(
        "/input", 400, {"detail": f"bad header: Bearer {CONTROL_TOKEN}"}
    )
    plant_session("s-1", port=control_plane.port)

    response = client.post("/api/sessions/s-1/wind-down", headers=bearer_header())

    assert CONTROL_TOKEN not in response.text
    assert SECRET not in response.text


def test_the_exit_route_ends_a_real_session(client, config, platform_root):
    result = live_session(config)
    try:
        response = client.post(
            f"/api/sessions/{result.session_id}/exit", headers=bearer_header()
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["verb"] == lifecycle.VERB_EXIT
        assert payload["signals"] == ["SIGTERM"]
        assert not alive(result.pid)
    finally:
        reap(result.pid)


def test_the_exit_route_relays_a_refusal_as_a_409(client, config, platform_root):
    result = live_session(config)
    try:
        reattach.mark_detached(
            result.session_id, output=reattach.OUTPUT_NONE, detail="host pty lost"
        )

        response = client.post(
            f"/api/sessions/{result.session_id}/exit", headers=bearer_header()
        )

        assert response.status_code == 409
        assert "wind" in response.json()["detail"].lower()
        assert alive(result.pid)
    finally:
        reap(result.pid)


def test_the_route_list_names_both_verbs_and_the_difference(client):
    body = client.get("/api", headers=bearer_header()).text

    assert "/wind-down" in body
    assert "/exit" in body
    assert "only when a human asks" in body


# --- the UI: two verbs, deliberately unequal --------------------------------
#
# Source-level, because this image has no JS runner and every way this goes wrong
# still renders a tidy card. The one that would quietly destroy the feature is
# giving the two verbs the same weight: it looks fine in a screenshot, and the
# blunt one is then a mis-aimed tap away on a phone.

def _detail():
    return RUN_DETAIL.read_text(encoding="utf-8")


def _without_comments(source):
    """The source minus ``//`` lines and ``<!-- -->`` blocks, for the hex scan."""
    source = re.sub(r"<!--.*?-->", "", source, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", source, flags=re.MULTILINE)


def _copy():
    """The component's source with runs of whitespace collapsed.

    Markup wraps where the line length says it does, so a sentence in a template is
    regularly split across lines. Asserting on the source verbatim would make these
    tests fail on a re-wrap, which teaches the next person to delete them.
    """
    return " ".join(_detail().split())


def _button(label):
    """The ``<v-btn>`` tag whose content is *label*, attributes and all."""
    match = re.search(
        r"<v-btn\b((?:[^>]|\n)*?)>\s*" + re.escape(label), _detail(), re.S
    )
    assert match, f"RunDetail.vue has no <v-btn> reading {label!r}"
    return match.group(1)


def test_the_ui_offers_both_verbs():
    copy = _copy()
    assert "wind down" in copy
    assert "exit now" in copy


def test_the_two_verbs_are_not_two_equal_buttons():
    """The operator's requirement, and D22's reason for it: wind down is the ordinary
    action and exit is the one you have to mean. Equal prominence is the failure."""
    wind = _button("wind down")
    end = _button("exit now")

    assert 'color="primary"' in wind, "wind down is the ordinary, safe action"
    assert 'size="large"' in wind
    assert 'color="error"' in end
    assert 'size="small"' in end
    assert 'size="large"' not in end


def test_exit_is_behind_a_confirmation():
    """The visible control opens a dialog; only the dialog's button ends anything.

    On a phone the difference between "reached for it" and "meant it" is one tap,
    which is the whole reason this dialog exists.
    """
    detail = _detail()
    assert "<v-dialog" in detail
    assert 'confirmingExit = true' in _button("exit now"), (
        "the subordinate control must open the dialog, not call the verb"
    )
    assert '@click="exitNow"' in detail
    assert re.search(r"<v-dialog(?:[^>]|\n)*?>(?:.|\n)*?exitNow", detail), (
        "the call that ends the session must live inside the dialog"
    )


def test_the_ui_says_what_exit_costs_before_it_is_tapped():
    """The argument for the default verb, in the words an operator reads: what is
    lost is the work, and what is lost with it may be the thing they asked to see."""
    copy = _copy()
    assert "nothing is committed" in copy
    assert "nothing is pushed" in copy
    assert "Wind it down instead" in copy


def test_the_ui_says_wind_down_ends_on_the_agents_schedule():
    """A wind-down that looks like it did nothing is a wind-down an operator
    follows with an exit thirty seconds later."""
    copy = _copy()
    assert "end the session itself" in copy
    assert "decides when it is done" in copy


def test_a_reattached_session_is_told_exit_is_unavailable():
    """The server refuses it (409); a button that always fails is still a bug."""
    detail = _detail()
    assert "exitBlocked" in detail
    assert "run.detached" in detail
    assert ':disabled="!canExit"' in _button("exit now")


def test_the_ui_surfaces_the_backstop_without_escalating_to_a_kill():
    """Spec R18: past the deadline something says so and the human chooses."""
    assert "backstop_at" in _detail()
    assert "windDownOverdue" in _detail()
    assert "nothing here will kill it for you" in _copy()


def test_the_ending_controls_are_only_offered_for_a_live_session():
    assert 'v-if="session && run.live"' in _detail()


def test_the_ui_posts_to_the_two_routes_it_documents():
    """Wherever the request helper ends up living, these are the paths."""
    sources = _detail() + API_CLIENT.read_text(encoding="utf-8")
    assert "'wind-down'" in sources
    assert "'exit'" in sources


def test_the_new_markup_hardcodes_no_colour_and_no_swept_out_variant():
    """The theme owns colour (one place, both schemes), and `outlined` and `flat`
    were swept out of every component at the operator's request as reading like an
    unfinished wireframe.

    The hex scan runs on the comment-stripped source: an MR reference in a comment
    ("MR #164") is hex-shaped to a regex, and a colour cannot act from inside a
    comment — code is where it would reach the screen.
    """
    detail = _without_comments(_detail())
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", detail)
    assert 'variant="outlined"' not in detail
    assert 'variant="flat"' not in detail


def test_the_icons_are_bundled_svg_paths():
    """@mdi/font would be a webfont fetched at first paint — on a LAN with no route
    out, an icon font that fails to load is a UI of empty boxes."""
    detail = _detail()
    assert "from '@mdi/js'" in detail
    assert "mdi-" not in detail
