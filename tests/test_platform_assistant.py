"""Tests for the orchestrating assistant's lifecycle (issue #141, T29; spec §8).

Sessions are started for real — against a stub standing in for ``lmer``, the same
one ``tests/test_platform_spawn.py`` uses — so the spawn path, the registry entry
and the liveness checks are genuinely exercised without launching a container.

The properties that matter: there is one assistant at a time and the *registry*
is what says so, a dead one is detected and replaced rather than mourned, the
rotation bookkeeping survives a reload, the assistant is spawned with
``kind="assistant"`` and no repository (D17), and every refusal is legible and
lands in platform history (§8.2).

T30 adds a second group at the bottom: the session is given a URL that works
*from inside a container* and the shared secret to use it with, or is told in
its own environment why it got neither. The credential must not reach argv, the
event log or the registry on the way.

T92 extends T87's credential scrub to the two documents beside the standing
orders in the same file — the handoff and the digest spool — in both directions:
what a credential reaches must be neither the file nor the reader.

Note the stub-lifetime trap this module inherits: a stub that exits cleanly has
its registry entry reaped by the watcher thread, so any assertion about a
*running* assistant sets ``FAKE_LMER_SLEEP`` and kills the process afterwards.
"""

import contextlib
import json
import os
import signal
import stat
import time

import pytest
from dotenv import dotenv_values

from lmer_cli import cli, resolve
from lmer_platform import assistant
from lmer_platform import config as cfg
from lmer_platform import registry, runs, spawn, store
from tests.conftest import strip_lmer_env


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in ("LMER_REPO_URL", "LMER_PLATFORM_PORTS_FILE", "FAKE_LMER_SLEEP",
                 "FAKE_LMER_EXIT", cfg.ENV_CONTAINER_URL, cfg.ENV_SECRET_FILE):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture
def fake_lmer(tmp_path):
    """A stub standing in for `lmer`: records its argv and environment, then exits.

    The environment is recorded because D17's mechanism travels in it (see
    :data:`lmer_platform.spawn.NO_REPO_ENV`) — asserting on the request the
    assistant built would only restate assistant.py, while ``env`` is what the
    process actually received.

    With ``FAKE_LMER_SLEEP`` it stays up instead — which is how a test keeps a
    registry entry around long enough to assert on it.
    """
    script = tmp_path / "fake-lmer"
    script.write_text(
        "#!/bin/sh\n"
        'echo "fake lmer started: $*"\n'
        f'env > "{tmp_path / "child-env.txt"}"\n'
        'if [ -n "$FAKE_LMER_SLEEP" ]; then sleep "$FAKE_LMER_SLEEP"; fi\n'
        'exit "${FAKE_LMER_EXIT:-0}"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def child_env(tmp_path):
    """The environment the stub was launched with, once it has run.

    Continuation lines of multi-line values are ignored, as in
    tests/test_platform_spawn.py's reader.
    """
    dump = tmp_path / "child-env.txt"
    assert wait_for(lambda: dump.is_file() and dump.stat().st_size), (
        "the assistant's child never started"
    )
    values = {}
    for line in dump.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        if sep and name:
            values[name] = value
    return values


@pytest.fixture
def config(platform_root, fake_lmer):
    return cfg.load({"lmer_bin": str(fake_lmer)})


@pytest.fixture
def long_lived(monkeypatch):
    """Keep the stub alive for the length of a test.

    A clean exit reaps the registry entry, and the registry is what this module
    reads to answer "is an assistant running" — so without this every assertion
    about a live assistant races the watcher thread.
    """
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")


def kill(pid):
    if isinstance(pid, int) and pid > 1:
        with contextlib.suppress(OSError):
            os.kill(pid, 9)


@contextlib.contextmanager
def started(config, **kwargs):
    """Start an assistant and make sure it is gone by the end of the test."""
    status = assistant.start(config, **kwargs)
    try:
        yield status
    finally:
        kill(status.pid)


def wait_for(predicate, timeout=5.0):
    """Poll until *predicate* holds — exits are recorded asynchronously."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def events_of(event_type):
    return [e for e in store.read_events() if e.get("type") == event_type]


# --- a fresh platform -------------------------------------------------------

def test_a_fresh_platform_has_no_assistant(platform_root):
    """Started on demand (D11): nothing exists until someone asks for one."""
    status = assistant.status()
    assert status.running is False
    assert status.session_id is None
    assert status.generation == 0
    assert status.stale is False
    assert status.pending == 0
    assert not assistant.state_path().exists()


def test_status_serialises_for_a_route(platform_root):
    payload = assistant.status().to_dict()
    assert payload["running"] is False
    assert payload["taskdef"] == assistant.TASKDEF
    assert payload["target"] == assistant.TARGET


# --- start / stop / status round trip ---------------------------------------

def test_start_stop_status_round_trip(config, long_lived):
    with started(config) as status:
        assert status.running is True
        assert status.session_id
        assert status.pid
        assert status.generation == 1
        assert status.tracked is True
        assert status.stale is False

        live = assistant.status()
        assert live.running is True
        assert live.session_id == status.session_id
        assert live.age_seconds is not None and live.age_seconds >= 0

        assert assistant.stop() is True

        after = assistant.status()
        assert after.running is False
        assert after.session_id is None
        assert after.stale is False, "a stop must not leave a pointer behind"
        assert after.generation == 1, "generation counts incarnations, not restarts"


def test_stop_removes_the_registry_entry_it_killed(config, long_lived):
    """A requested exit is not a crash, and must not read as one.

    ``spawn`` keeps the entry of a session that exited unclean because that entry
    is the crash signal — and a signalled exit is never clean, so nothing else
    would ever clear this one.
    """
    with started(config) as status:
        assert assistant.stop() is True
        assert registry.read_session(status.session_id) is None


def test_stop_when_nothing_runs_says_so(platform_root):
    assert assistant.stop() is False


def test_start_records_platform_history(config, long_lived):
    with started(config) as status:
        started_events = events_of("assistant_started")
        assert started_events, "starting the assistant must be visible in history"
        assert started_events[-1]["data"]["session"] == status.session_id
        assert started_events[-1]["data"]["generation"] == 1

        assistant.stop(reason="rotation")
        stopped = events_of("assistant_stopped")[-1]
        assert stopped["data"]["reason"] == "rotation"
        assert stopped["data"]["stopped"] is True


# --- one at a time ----------------------------------------------------------

def test_only_one_assistant_at_a_time(config, long_lived):
    with started(config) as status:
        with pytest.raises(assistant.AssistantAlreadyRunning) as caught:
            assistant.start(config)
        assert status.session_id in str(caught.value)
        assert caught.value.status == 409
        assert events_of("assistant_start_refused")[-1]["data"]["reason"] == (
            "already_running"
        )


def test_ensure_running_is_idempotent(config, long_lived):
    with started(config) as status:
        again = assistant.ensure_running(config)
        assert again.session_id == status.session_id
        assert again.generation == 1
        live = [
            e for e in registry.list_sessions(live_only=True)
            if e.get("kind") == assistant.KIND
        ]
        assert len(live) == 1


def test_a_running_worker_is_not_an_assistant(config, platform_root):
    """Liveness is read per kind: a busy fleet must not look like an assistant."""
    registry.register("s-worker", kind="worker", pid=os.getpid())
    assert assistant.status().running is False


def test_the_recorded_assistant_wins_when_two_are_live(platform_root):
    """A stray second assistant must not make status flip session ids."""
    registry.register("s-first", kind=assistant.KIND, pid=os.getpid(),
                      started_at="2026-07-27T09:00:00Z")
    registry.register("s-second", kind=assistant.KIND, pid=os.getpid(),
                      started_at="2026-07-27T10:00:00Z")
    store.write_json(assistant.state_path(), {"session_id": "s-second"})

    status = assistant.status()
    assert status.running is True
    assert status.session_id == "s-second"
    assert status.tracked is True


def test_an_untracked_live_assistant_is_reported_as_such(platform_root):
    """A daemon restart that lost its pointer still finds the assistant.

    And still knows how old it is: an adopted assistant has no recorded start,
    so the age a rotation policy reads has to come off the live entry.
    """
    registry.register("s-orphan", kind=assistant.KIND, pid=os.getpid(),
                      started_at="2026-07-27T09:00:00Z")
    status = assistant.status()
    assert status.running is True
    assert status.session_id == "s-orphan"
    assert status.tracked is False
    assert status.started_at == "2026-07-27T09:00:00Z"
    assert status.age_seconds is not None


def test_an_unreaped_session_is_not_waited_on(platform_root):
    """The daemon is every session's parent, so its dead children are zombies.

    A zombie still answers ``kill(pid, 0)``. Counting one as alive would make a
    stop spin out its whole grace period, escalate to SIGKILL against a corpse,
    and then report failure for a session that is definitively gone.
    """
    import subprocess

    child = subprocess.Popen(["/bin/true"])
    try:
        assert wait_for(lambda: registry._is_zombie(child.pid)), (
            "expected an unreaped zombie"
        )
        os.kill(child.pid, 0)  # still findable — that is the whole trap
        assert assistant._alive(child.pid) is False
        assert assistant._wait_gone(child.pid, 0.1) is True
    finally:
        child.wait()


# --- a dead assistant is detected and replaced -------------------------------

def test_a_dead_assistant_is_detected(config, long_lived):
    with started(config) as status:
        kill(status.pid)
        assert wait_for(lambda: assistant.status().running is False)

        dead = assistant.status()
        assert dead.stale is True, "state still names a session that is gone"
        assert dead.session_id == status.session_id


def test_a_dead_assistant_can_be_replaced(config, long_lived):
    with started(config) as first:
        kill(first.pid)
        assert wait_for(lambda: assistant.status().running is False)

        second = assistant.ensure_running(config)
        try:
            assert second.running is True
            assert second.session_id != first.session_id
            assert second.generation == 2, "each incarnation advances the counter"
            assert second.stale is False
        finally:
            kill(second.pid)


def test_replacing_a_dead_assistant_carries_the_handoff_forward(config, long_lived):
    """A crash must leave its successor as informed as a planned rotation."""
    with started(config, handoff="mr-163 is waiting on review") as first:
        kill(first.pid)
        assert wait_for(lambda: assistant.status().running is False)

        second = assistant.ensure_running(config)
        try:
            assert second.handoff == "mr-163 is waiting on review"
        finally:
            kill(second.pid)


def test_stop_clears_a_stale_pointer(config, long_lived):
    with started(config) as status:
        kill(status.pid)
        assert wait_for(lambda: assistant.status().running is False)

        assert assistant.stop() is False
        cleared = assistant.status()
        assert cleared.stale is False
        assert cleared.session_id is None


def test_stop_refuses_to_signal_its_own_process(platform_root, caplog):
    """The pid comes out of a file an operator can edit; 'me' is a real value."""
    registry.register("s-self", kind=assistant.KIND, pid=os.getpid())
    assert assistant.stop() is False
    assert registry.read_session("s-self") is not None
    assert any("platform_assistant_self_pid" in r.message for r in caplog.records)


def test_a_group_leader_is_signalled_as_a_group(platform_root, monkeypatch):
    """``lmer``'s own child holds the container; signalling only the parent
    leaves a container running with nothing left watching it."""
    calls = []
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(os, "killpg", lambda *args: calls.append(("killpg", *args)))
    monkeypatch.setattr(os, "kill", lambda *args: calls.append(("kill", *args)))

    assert assistant._signal_group(4242, signal.SIGTERM) is True
    assert calls == [("killpg", 4242, signal.SIGTERM)]


def test_a_pid_that_leads_no_group_is_signalled_alone(platform_root, monkeypatch):
    """``killpg`` on a pid that is not a group id signals a group we never started."""
    calls = []
    monkeypatch.setattr(os, "getpgid", lambda pid: pid + 1)
    monkeypatch.setattr(os, "killpg", lambda *args: calls.append(("killpg", *args)))
    monkeypatch.setattr(os, "kill", lambda *args: calls.append(("kill", *args)))

    assert assistant._signal_group(4242, signal.SIGTERM) is True
    assert calls == [("kill", 4242, signal.SIGTERM)]


def test_a_process_already_gone_is_not_signalled(platform_root, monkeypatch):
    def lookup_fails(_pid):
        raise ProcessLookupError

    monkeypatch.setattr(os, "getpgid", lookup_fails)
    monkeypatch.setattr(os, "killpg", lambda *a: pytest.fail("must not signal"))
    monkeypatch.setattr(os, "kill", lambda *a: pytest.fail("must not signal"))

    assert assistant._signal_group(4242, signal.SIGTERM) is False


def test_a_session_that_ignores_sigterm_is_killed(config, monkeypatch, tmp_path):
    """The escalation is what makes ``stop`` mean stopped.

    Without it ``stop`` returns while the session is still up, and the ``start``
    an operator issues next is refused as "already running".
    """
    stubborn = tmp_path / "stubborn-lmer"
    stubborn.write_text(
        "#!/bin/sh\n"
        'trap "" TERM\n'
        "while :; do sleep 1; done\n",
        encoding="utf-8",
    )
    stubborn.chmod(0o755)
    monkeypatch.setattr(assistant, "STOP_GRACE_SECONDS", 0.3)
    stubborn_config = cfg.load({"lmer_bin": str(stubborn)})

    with started(stubborn_config) as status:
        assert assistant.stop() is True
        assert assistant.status().running is False


@pytest.mark.parametrize("pid", [0, 1, -1, True, None, "1234"])
def test_terminate_refuses_pids_that_cannot_name_a_session(
    platform_root, pid, monkeypatch
):
    """0 and -1 mean "every process I can signal" to ``kill``; 1 is init.

    The signal calls are stubbed rather than trusted, because the failure this
    guards against is not survivable: with the guard removed, ``os.kill(0, …)``
    signals the whole process group — the test runner included — so a regression
    has to surface as an assertion rather than as a suite that vanishes. (Found
    the hard way while mutation-checking this exact guard.)
    """
    signalled = []
    monkeypatch.setattr(os, "kill", lambda *args: signalled.append(args))
    monkeypatch.setattr(os, "killpg", lambda *args: signalled.append(args))

    assert assistant._terminate(pid) is False
    assert signalled == [], f"no signal may be sent for pid {pid!r}"


# --- rotation ---------------------------------------------------------------

def test_rotation_replaces_the_session_and_carries_the_summary(config, long_lived):
    with started(config) as first:
        rotated = assistant.rotate(config, handoff="3 runs live, 1 blocked on you")
        try:
            assert rotated.running is True
            assert rotated.session_id != first.session_id
            assert rotated.generation == 2
            assert rotated.handoff == "3 runs live, 1 blocked on you"
            assert assistant.read_state().stop_reason is None, (
                "the new incarnation is running, not stopped"
            )
        finally:
            kill(rotated.pid)

    assert events_of("assistant_stopped")[-1]["data"]["reason"] == "rotation"


def test_set_handoff_is_readable_by_the_next_incarnation(config, long_lived):
    assistant.set_handoff("waiting on the operator about the schema bump")
    with started(config) as status:
        assert status.handoff == "waiting on the operator about the schema bump"


def test_age_is_derivable_because_a_rotation_policy_needs_it(config, long_lived):
    with started(config):
        age = assistant.status().age_seconds
        assert age is not None and 0 <= age < 60


def test_age_tolerates_an_unparseable_timestamp(platform_root):
    store.write_json(assistant.state_path(), {"started_at": "yesterday"})
    assert assistant.status().age_seconds is None


def test_a_future_start_reads_as_a_negative_age(platform_root):
    """Unclamped on purpose: a plausible-looking 0 would hide a wrong clock."""
    store.write_json(assistant.state_path(), {"started_at": "2099-01-01T00:00:00Z"})
    age = assistant.status().age_seconds
    assert age is not None and age < 0


@pytest.mark.parametrize("reason", ["", "shutdown", None, 3])
def test_stop_rejects_an_unknown_reason(platform_root, reason):
    with pytest.raises(assistant.AssistantError, match="invalid stop reason"):
        assistant.stop(reason=reason)


# --- persisted state --------------------------------------------------------

def test_state_survives_a_reload(config, long_lived):
    with started(config, handoff="one run blocked on a question") as status:
        assistant.notify("develop-issue-141 stopped on a question")

        on_disk = json.loads(assistant.state_path().read_text(encoding="utf-8"))
        assert on_disk["schema"] == store.SCHEMA_VERSION
        assert on_disk["session_id"] == status.session_id
        assert on_disk["generation"] == 1
        assert on_disk["handoff"] == "one run blocked on a question"
        assert len(on_disk["pending"]) == 1

        reloaded = assistant.read_state()
        assert reloaded.session_id == status.session_id
        assert reloaded.generation == 1
        assert reloaded.handoff == "one run blocked on a question"
        assert reloaded.pending[0].note == "develop-issue-141 stopped on a question"


def test_a_corrupt_state_file_does_not_take_the_assistant_down(platform_root):
    assistant.state_path().parent.mkdir(parents=True, exist_ok=True)
    assistant.state_path().write_text("{not json", encoding="utf-8")

    assert assistant.read_state() == assistant.AssistantState()
    backups = list(platform_root.glob("assistant.json.bad-*"))
    assert backups, "the unparseable bytes must be kept for post-mortem"


@pytest.mark.parametrize("payload", [
    {"generation": "many"}, {"generation": -1}, {"generation": True},
    {"pending": "nope"}, {"pending": [1, None, {"note": ""}]},
    {"session_id": 17}, {"handoff": []},
])
def test_a_hand_edited_state_file_costs_one_value(platform_root, payload):
    """Plain files are only worth it if a typo does not empty the whole model."""
    store.write_json(assistant.state_path(), payload)
    state = assistant.read_state()
    assert state.generation == 0
    assert state.pending == ()
    assert state.session_id is None
    assert state.handoff is None


def test_a_state_write_failure_does_not_kill_a_live_assistant(
    config, long_lived, monkeypatch
):
    """The registry is the authority; losing the pointer is a bookkeeping loss."""
    monkeypatch.setattr(
        assistant, "write_json",
        lambda *_a, **_k: (_ for _ in ()).throw(store.StoreError("disk full")),
    )
    with started(config) as status:
        assert status.running is True
        assert registry.read_session(status.session_id) is not None


# --- the notification seam (§8.3) -------------------------------------------

def test_notify_spools_for_an_assistant_that_is_not_up_yet(platform_root):
    """The daemon detects whether or not an assistant exists — it must not lose it."""
    assert assistant.notify("mr-163 has new findings") is False
    state = assistant.read_state()
    assert [note.note for note in state.pending] == ["mr-163 has new findings"]


def test_notify_reports_that_one_is_live(config, long_lived):
    with started(config):
        assert assistant.notify("develop-issue-141 opened an MR") is True


def test_notify_records_the_digest_in_history(platform_root):
    assistant.notify("s-1 crashed", kind="crashed", data={"session": "s-1"})
    event = events_of("assistant_notified")[-1]
    assert event["data"]["kind"] == "crashed"


def test_the_spool_is_bounded(platform_root, caplog):
    """A queue nobody drains is a memory leak with a filename.

    Asserted against the *file*, not against ``read_state``: the reader trims
    too, so a read-side assertion here would pass with the write-side bound
    removed and the state file growing without limit.
    """
    for index in range(assistant.MAX_PENDING + 5):
        assistant.notify(f"event {index}")

    on_disk = json.loads(assistant.state_path().read_text(encoding="utf-8"))
    assert len(on_disk["pending"]) == assistant.MAX_PENDING
    assert on_disk["pending"][0]["note"] == "event 5", (
        "the oldest digests are the ones dropped"
    )
    assert on_disk["pending"][-1]["note"] == f"event {assistant.MAX_PENDING + 4}"
    assert any(
        "platform_assistant_digest_dropped" in r.message for r in caplog.records
    )


def test_an_over_long_spool_is_trimmed_on_read(platform_root):
    """The other half of the bound, for a file this process did not write."""
    store.write_json(assistant.state_path(), {
        "pending": [
            {"at": "2026-07-27T09:00:00Z", "kind": "event", "note": f"n{i}"}
            for i in range(assistant.MAX_PENDING + 10)
        ],
    })
    pending = assistant.read_state().pending
    assert len(pending) == assistant.MAX_PENDING
    assert pending[-1].note == f"n{assistant.MAX_PENDING + 9}"


def test_take_pending_drains(platform_root):
    assistant.notify("first")
    assistant.notify("second")

    drained = assistant.take_pending()
    assert [note["note"] for note in drained] == ["first", "second"]
    assert assistant.take_pending() == []
    assert assistant.status().pending == 0


@pytest.mark.parametrize("note", ["", "   ", None, 42, b"bytes"])
def test_notify_rejects_a_note_that_is_not_text(platform_root, note):
    with pytest.raises(assistant.AssistantError, match="note must be non-empty"):
        assistant.notify(note)


def test_notify_rejects_an_oversized_note(platform_root):
    with pytest.raises(assistant.AssistantError, match="over the"):
        assistant.notify("x" * (assistant.MAX_NOTE_CHARS + 1))


def test_notify_rejects_an_unusable_kind(platform_root):
    with pytest.raises(assistant.AssistantError, match="kind must be non-empty"):
        assistant.notify("something happened", kind="")


def test_handoff_is_bounded(platform_root):
    """It is a compact summary handed to a fresh window, not a transcript."""
    with pytest.raises(assistant.AssistantError, match="over the"):
        assistant.set_handoff("x" * (assistant.MAX_HANDOFF_CHARS + 1))


def test_a_credential_in_a_digest_note_is_scrubbed_before_it_is_stored(platform_root):
    """A digest is daemon-composed, not daemon-authored: it quotes a session.

    ``detect.Signal.digest`` puts the question a run asked into the note, so the
    text an agent wrote reaches this file through here — and it leaves again
    through ``POST /api/assistant/pending``, so the scrub has to be on the way in
    rather than at the reader's discretion.
    """
    assistant.notify(
        "s-1 asks whether to clone "
        "https://oauth2:leaked-digest@gitlab.example.com/grp/proj.git"
    )
    on_disk = assistant.state_path().read_text(encoding="utf-8")
    assert "leaked-digest" not in on_disk

    drained = assistant.take_pending()
    assert "leaked-digest" not in drained[0]["note"]
    assert "gitlab.example.com/grp/proj.git" in drained[0]["note"], (
        "the scrub took the credential, not the digest"
    )


def test_the_note_scrub_runs_before_the_bound(platform_root):
    """The spool's char bound, checked on what would be stored — as for the orders.

    Masking lengthens the text, so a note at exactly the limit grows past it.
    Bounded first, this would be accepted and the file would end up over the
    limit, with the refusal naming a length nobody stored.
    """
    header = "Authorization: Bearer y"
    at_the_limit = "x" * (assistant.MAX_NOTE_CHARS - len(header)) + header
    assert len(at_the_limit) == assistant.MAX_NOTE_CHARS

    with pytest.raises(assistant.AssistantError) as caught:
        assistant.notify(at_the_limit)
    assert str(assistant.MAX_NOTE_CHARS + 9) in str(caught.value)
    assert assistant.read_state().pending == ()


def test_a_credential_in_a_spooled_note_is_not_served_either(platform_root):
    """A file this process did not write is still drained clean.

    Hand-edited, or written by a build older than the scrub: the read half is what
    makes "a credential in a digest is neither stored nor served" true of both.
    """
    store.write_json(assistant.state_path(), {
        "pending": [{
            "at": "2026-07-28T09:00:00Z",
            "kind": "asking",
            "note": "s-1 tried Authorization: Bearer glpat-spoolleak",
        }],
    })

    drained = assistant.take_pending()
    assert "glpat-spoolleak" not in drained[0]["note"]
    assert "<redacted>" in drained[0]["note"]


def test_the_scrub_does_not_change_what_counts_against_the_spool_bound(platform_root):
    """Eviction is unchanged by masking, and a scrubbed spool still round-trips.

    The spool's bound counts notes rather than characters, so a mask that changes
    a note's length must not change which digests survive — the oldest are still
    the ones dropped, and what is drained is what is on disk.
    """
    for index in range(assistant.MAX_PENDING + 5):
        assistant.notify(
            f"event {index} on "
            f"https://oauth2:leaked-{index}@gitlab.example.com/grp/proj.git"
        )

    on_disk = json.loads(assistant.state_path().read_text(encoding="utf-8"))
    assert len(on_disk["pending"]) == assistant.MAX_PENDING
    assert on_disk["pending"][0]["note"].startswith("event 5"), (
        "the oldest digests are the ones dropped"
    )
    assert "leaked-" not in json.dumps(on_disk["pending"])

    drained = assistant.take_pending()
    assert [note["note"] for note in drained] == [
        note["note"] for note in on_disk["pending"]
    ]


def test_a_credential_in_a_digest_payload_is_scrubbed_before_it_is_stored(platform_root):
    """The digest's other half is quoted text too (T93).

    ``detect.Signal.data`` copies a fleet row into the payload, so the ``label`` in
    it is the branch or MR title an *agent* chose — the same provenance as the note
    beside it, and it leaves through the same route.
    """
    assistant.notify(
        "mr-168 has findings",
        kind="review_ready",
        data={
            "slug": "mr-168",
            "label": "clone with Authorization: Bearer glpat-payloadleak",
        },
    )
    on_disk = assistant.state_path().read_text(encoding="utf-8")
    assert "glpat-payloadleak" not in on_disk

    drained = assistant.take_pending()
    assert "glpat-payloadleak" not in json.dumps(drained[0]["data"])
    assert "<redacted>" in drained[0]["data"]["label"]
    assert drained[0]["data"]["slug"] == "mr-168", (
        "the scrub took the credential, not the payload"
    )


def test_a_digest_payload_is_scrubbed_value_by_value(platform_root):
    """Recursed over decoded values, never applied to the serialised payload.

    Some of these patterns do not end at a quote, so one let loose on a JSON line
    can run past a closing quote and eat the key after it — leaving a record that
    still parses and no longer says what the daemon wrote. The nesting is here
    because that is where a shortcut would show.
    """
    assistant.notify(
        "s-1 is asking",
        data={
            "slug": "s-1",
            "attention": {
                "reason": "asking",
                "note": (
                    "cloning "
                    "https://oauth2:glpat-nestedleak@gitlab.example.com/grp/proj.git"
                ),
            },
            "refs": ["Authorization: Bearer glpat-listleak", "mr-168"],
            "since": None,
            "rounds": 3,
        },
    )

    payload = assistant.take_pending()[0]["data"]
    assert "glpat-nestedleak" not in json.dumps(payload)
    assert "glpat-listleak" not in json.dumps(payload)
    assert payload["attention"]["reason"] == "asking"
    assert payload["refs"][1] == "mr-168"
    assert payload["since"] is None and payload["rounds"] == 3, (
        "a non-string value is left as it was"
    )


def test_a_credential_in_a_spooled_payload_is_not_served_either(platform_root):
    """The read half, for a file this process did not write — as for the note."""
    store.write_json(assistant.state_path(), {
        "pending": [{
            "at": "2026-07-28T09:00:00Z",
            "kind": "review_ready",
            "note": "mr-168 has findings",
            "data": {"label": "pushed with Authorization: Bearer glpat-storedleak"},
        }],
    })

    drained = assistant.take_pending()
    assert "glpat-storedleak" not in json.dumps(drained[0]["data"])
    assert "<redacted>" in drained[0]["data"]["label"]


def test_an_unusable_digest_payload_is_still_dropped(platform_root):
    """The scrub replaced a type check and has to keep answering for it."""
    assistant.notify("mr-168 has findings", data=["not", "a", "mapping"])
    assert assistant.read_state().pending[0].data is None


def test_the_payload_scrub_does_not_change_what_the_spool_evicts(platform_root):
    """Masking a payload must not move the bound, as for the note (T92).

    The spool counts notes rather than characters, so the oldest digests are still
    the ones dropped — and what is drained is what is on disk, payloads included.
    """
    for index in range(assistant.MAX_PENDING + 5):
        assistant.notify(
            f"event {index}",
            data={"label": f"branch with Authorization: Bearer glpat-evict{index}"},
        )

    on_disk = json.loads(assistant.state_path().read_text(encoding="utf-8"))
    assert len(on_disk["pending"]) == assistant.MAX_PENDING
    assert on_disk["pending"][0]["note"] == "event 5", (
        "the oldest digests are the ones dropped"
    )
    assert "glpat-evict" not in json.dumps(on_disk["pending"])

    drained = assistant.take_pending()
    assert [note["data"] for note in drained] == [
        note["data"] for note in on_disk["pending"]
    ]


def test_a_credential_in_the_handoff_is_scrubbed_before_it_is_stored(platform_root):
    """The handoff is agent-authored text in the same file as the standing orders.

    Written by an incarnation summarising a conversation for its successor, so
    "reach the forge with this token" arrives here for T87's reason — and it is
    served back verbatim to the next incarnation and to a browser.
    """
    state = assistant.set_handoff(
        "mr-168 is waiting; the forge wants Authorization: Bearer glpat-handoffleak"
    )
    assert "glpat-handoffleak" not in state.handoff
    assert "<redacted>" in state.handoff
    assert "glpat-handoffleak" not in assistant.state_path().read_text(
        encoding="utf-8"
    )
    assert "glpat-handoffleak" not in assistant.status().handoff


def test_a_credential_in_a_lifecycle_handoff_is_scrubbed(platform_root):
    """``start`` and ``stop`` take a handoff too, and a rotation goes through both.

    Scrubbing only :func:`assistant.set_handoff` would leave the path a *rotation*
    uses — the one case the handoff exists for — writing the raw text.
    """
    assert assistant.stop(
        handoff="hand over to the next window with Authorization: Bearer glpat-rotleak"
    ) is False

    assert "glpat-rotleak" not in assistant.read_state().handoff
    assert "glpat-rotleak" not in assistant.state_path().read_text(encoding="utf-8")


def test_the_handoff_scrub_runs_before_the_bound(platform_root):
    """Masking lengthens text, so the bound is checked on what would be stored."""
    header = "Authorization: Bearer y"
    at_the_limit = "x" * (assistant.MAX_HANDOFF_CHARS - len(header)) + header
    assert len(at_the_limit) == assistant.MAX_HANDOFF_CHARS

    with pytest.raises(assistant.AssistantError) as caught:
        assistant.set_handoff(at_the_limit)
    assert str(assistant.MAX_HANDOFF_CHARS + 9) in str(caught.value), (
        "the refusal counts the characters that were sent, not the ones that would "
        "have been stored"
    )
    assert assistant.read_state().handoff is None


def test_a_credential_in_a_stored_handoff_is_not_served_either(platform_root):
    """Both directions, one definition — and the next write cleans the file.

    The scrub is at the rebuild point rather than at each of the three places the
    handoff is served (the route, the status, a start carrying it forward), which
    is also what makes the masked text what the next write persists instead of
    leaving the credential in the file until somebody notices.
    """
    store.write_json(assistant.state_path(), {
        "handoff": "resume with https://oauth2:leaked-handoff@gitlab.example.com/g/p.git",
    })

    assert "leaked-handoff" not in assistant.read_state().handoff
    assert "leaked-handoff" not in assistant.status().handoff

    assistant.notify("anything that rewrites the file")
    assert "leaked-handoff" not in assistant.state_path().read_text(encoding="utf-8")


# --- the operator's standing orders (T87) ------------------------------------
#
# The handoff's sibling, and the tests are about the one property that differs:
# nothing consumes this document, so it has to survive every transition the
# lifecycle has — a stop, a start, and a rotation — and it has to stay a separate
# document from the handoff rather than a second name for it.

def test_standing_instructions_are_read_back_as_written(platform_root):
    state = assistant.set_instructions(
        "always spawn reviewers with the sol preset"
    )
    assert state.instructions == "always spawn reviewers with the sol preset"
    assert state.instructions_at
    assert assistant.read_state().instructions == (
        "always spawn reviewers with the sol preset"
    )


def test_a_fresh_host_has_no_standing_instructions(platform_root):
    """Absent reads as empty, which is what every host does today — the mixed-fleet
    property: an older assistant image never asks, and nothing changes for it."""
    assert assistant.read_state().instructions is None
    assert assistant.read_state().instructions_at is None


def test_standing_instructions_survive_a_stop_and_a_start(config, long_lived):
    """The whole point of a *standing* document: it is not a baton.

    A stop and a start are where the state file is rewritten, so this is where a
    field that is not carried forward would be lost — and the loss would be
    silent, showing up as an incarnation that has quietly stopped following the
    operator's orders.
    """
    assistant.set_instructions("never wind a run down without asking me")

    with started(config) as first:
        assert first.running is True
        assert assistant.read_state().instructions == (
            "never wind a run down without asking me"
        )
        assert assistant.stop() is True

    assert assistant.read_state().instructions == (
        "never wind a run down without asking me"
    ), "a stop dropped the operator's standing orders"

    with started(config) as second:
        assert second.running is True
        assert assistant.read_state().instructions == (
            "never wind a run down without asking me"
        ), "the next incarnation starts without the orders it must follow"


def test_standing_instructions_survive_a_rotation(config, long_lived):
    """Rotation is the transition that exists *because* a window filled, which is
    exactly when the operator's standing orders are the only record of them."""
    assistant.set_instructions("always tell me before spawning anything")

    with started(config):
        rotated = assistant.rotate(config, handoff="one run blocked")
        try:
            assert rotated.generation == 2
            state = assistant.read_state()
            assert state.instructions == "always tell me before spawning anything"
            assert state.handoff == "one run blocked"
        finally:
            kill(rotated.pid)


def test_the_handoff_and_the_standing_orders_are_independent_documents(platform_root):
    """Two writers, two fields, in both directions.

    They are read in the same startup breath and one is consumed while the other
    is not, so storing either in the other's field would look right for exactly
    one incarnation.
    """
    assistant.set_instructions("always use the sol preset for reviews")
    assistant.set_handoff("mr-168 is waiting on review")

    state = assistant.read_state()
    assert state.instructions == "always use the sol preset for reviews"
    assert state.handoff == "mr-168 is waiting on review"

    assistant.set_handoff("mr-168 merged; nothing in flight")
    assert assistant.read_state().instructions == (
        "always use the sol preset for reviews"
    ), "writing a handover note overwrote the operator's standing orders"

    assistant.set_instructions("also: never rotate me while a run is waiting")
    assert assistant.read_state().handoff == "mr-168 merged; nothing in flight", (
        "storing standing orders overwrote the handover note"
    )


def test_standing_instructions_are_bounded(platform_root):
    """Every future incarnation pays to read this, so it cannot become a diary."""
    with pytest.raises(assistant.AssistantError, match="over the"):
        assistant.set_instructions("x" * (assistant.MAX_INSTRUCTIONS_CHARS + 1))
    assert assistant.read_state().instructions is None


def test_an_oversized_document_does_not_clear_the_stored_one(platform_root):
    assistant.set_instructions("always ask before spawning")
    with pytest.raises(assistant.AssistantError, match="over the"):
        assistant.set_instructions("x" * (assistant.MAX_INSTRUCTIONS_CHARS + 1))
    assert assistant.read_state().instructions == "always ask before spawning"


def test_the_bound_is_the_same_magnitude_as_the_handoffs(platform_root):
    """Tighter than the handoff, and in the same order of magnitude as it.

    Tighter because a handoff is read once by one successor while this is read by
    every incarnation forever; the same magnitude because both are compact
    documents and a bound an order out either way would be a different decision
    (a paragraph, or a transcript) made silently.
    """
    assert assistant.MAX_INSTRUCTIONS_CHARS <= assistant.MAX_HANDOFF_CHARS
    assert assistant.MAX_INSTRUCTIONS_CHARS * 10 > assistant.MAX_HANDOFF_CHARS


@pytest.mark.parametrize("text", ["", "   ", None, 7, ["a rule"]])
def test_standing_instructions_reject_an_unusable_document(platform_root, text):
    """No clearing through this path: an empty POST is a composer bug far more
    often than an operator with no preferences, and orders that silently emptied
    are invisible until an incarnation does the thing it was told to stop."""
    with pytest.raises(
        assistant.AssistantError, match="instructions must be non-empty"
    ):
        assistant.set_instructions(text)


def test_a_credential_in_the_standing_orders_is_scrubbed_before_it_is_stored(
    platform_root
):
    """The operator is typing into a chat window, so "always use this token" will
    be said — and this lands in a plain file the daemon reads at every start.

    Scrubbed on the way in rather than only on the way out, because the file is
    where the credential would otherwise sit.
    """
    state = assistant.set_instructions(
        'always call the forge with Authorization: Bearer glpat-notarealtoken'
    )
    assert "glpat-notarealtoken" not in state.instructions
    assert "<redacted>" in state.instructions
    assert "glpat-notarealtoken" not in assistant.state_path().read_text(
        encoding="utf-8"
    )


def test_the_scrub_runs_before_the_bound(platform_root):
    """Masking can *lengthen* text, so the bound has to be checked on what is stored.

    A short credential becomes ``<redacted>``, which is longer than it was — so a
    document at exactly the limit grows past it. Bounded first and scrubbed
    afterwards, this would be accepted and the file would end up over the limit,
    and the character count in the refusal would name a length nobody stored.
    """
    header = "Authorization: Bearer y"
    at_the_limit = "x" * (assistant.MAX_INSTRUCTIONS_CHARS - len(header)) + header
    assert len(at_the_limit) == assistant.MAX_INSTRUCTIONS_CHARS

    with pytest.raises(assistant.AssistantError) as caught:
        assistant.set_instructions(at_the_limit)
    assert str(assistant.MAX_INSTRUCTIONS_CHARS + 9) in str(caught.value), (
        "the refusal counts the characters that were sent, not the ones that would "
        "have been stored"
    )
    assert assistant.read_state().instructions is None


def test_a_hand_edited_instructions_field_costs_one_value(platform_root):
    """Same tolerance as every other field: a typo costs the value, not the file."""
    store.write_json(
        assistant.state_path(), {"instructions": [], "generation": 3}
    )
    state = assistant.read_state()
    assert state.instructions is None
    assert state.generation == 3


@pytest.mark.parametrize("text", ["", "   ", None, 7])
def test_handoff_rejects_empty_text(platform_root, text):
    with pytest.raises(assistant.AssistantError, match="handoff must be non-empty"):
        assistant.set_handoff(text)


def test_start_rejects_an_unusable_handoff_before_spawning(config, long_lived):
    with pytest.raises(assistant.AssistantError):
        assistant.start(config, handoff="x" * (assistant.MAX_HANDOFF_CHARS + 1))
    assert registry.list_sessions(live_only=True) == [], (
        "a refused start must not leave a container behind"
    )


# --- what the spawn actually is ---------------------------------------------

def test_the_spawn_is_an_assistant_with_the_orchestrate_taskdef(config, long_lived):
    with started(config) as status:
        entry = registry.read_session(status.session_id)
        assert entry is not None
        assert entry["kind"] == "assistant"
        assert entry["task"]["taskdef"] == assistant.TASKDEF
        assert entry["task"]["target"] == assistant.TARGET
        assert entry["control"]["port"], "spec D8: every spawned session is drivable"


def test_the_assistant_is_spawned_without_a_repository(config, long_lived):
    """D17, structurally: no repo in the request, so no repo in the run."""
    with started(config) as status:
        entry = registry.read_session(status.session_id)
        assert entry["task"]["repo"] is None
        assert entry["run"]["host"] is None and entry["run"]["project"] is None
        assert runs.list_tracked() == [], (
            "an assistant has no run to file under any repository"
        )

        # What the child was actually launched with — the stub echoes its argv,
        # so a repository sneaking into the command line fails here rather than
        # being asserted about in a comment.
        # Wait for the CONTENT, not just the file. The drain thread creates the log
        # before the stub has written its first line, so waiting on existence alone
        # can read an empty file — green on an idle machine and red under load,
        # which is how it failed in a gate run. The marker below is the thing being
        # asserted, so waiting for it waits for exactly the right event.
        marker = f"fake lmer started: {assistant.TASKDEF} {assistant.TARGET}"
        log = spawn.log_path_for(status.session_id)
        assert wait_for(
            lambda: log.is_file() and marker in log.read_text(encoding="utf-8")
        ), f"the stub never announced itself in {log}"
        launched = log.read_text(encoding="utf-8")
        assert marker in launched
        assert "://" not in launched, "no repository URL may reach the child"


def test_the_assistant_target_cannot_name_a_repository(platform_root, tmp_path):
    """The fail-closed half of D17.

    ``lmer`` resolves a non-empty target that is not URL-shaped as a *local path*
    and refuses when it is not a checkout — it never falls back to inferring a
    repo from the daemon's working directory, which is the one way this session
    could come up with code in ``/workspace``. Make the target URL-shaped, or
    empty, and that stops being true.
    """
    assert not resolve.is_likely_url(assistant.TARGET)
    assert "/" not in assistant.TARGET and os.sep not in assistant.TARGET
    with pytest.raises(resolve.ResolveError):
        resolve.normalize_repo_url(assistant.TARGET, tmp_path, None)


def test_the_orchestrate_taskdef_ships_with_the_platform(platform_root):
    """The daemon and the prompt it runs are released together."""
    found = assistant.taskdef_dir()
    assert found is not None, "taskdef/orchestrate/instructions.txt must exist"
    assert (found / "instructions.txt").is_file()


# --- refusals ---------------------------------------------------------------

def test_start_refuses_when_the_taskdef_is_missing(config, tmp_path, monkeypatch):
    empty = tmp_path / "taskdefs"
    (empty / "chat").mkdir(parents=True)
    (empty / "chat" / "instructions.txt").write_text("hi", encoding="utf-8")
    monkeypatch.setattr(assistant, "_get_taskdef_paths", lambda _root: [empty])

    with pytest.raises(assistant.TaskdefMissing) as caught:
        assistant.start(config)
    assert caught.value.status == 503
    assert str(empty) in str(caught.value)
    assert registry.list_sessions(live_only=True) == []
    assert events_of("assistant_start_refused")[-1]["data"]["reason"] == (
        "taskdef_missing"
    )


def test_an_installed_host_defers_to_the_container(config, long_lived, monkeypatch):
    """With no host-side taskdef directory, the container's hook is authoritative.

    Mirrors what ``lmer`` itself does — its host-side task list is advisory
    because work-repo taskdefs are only visible inside the container — and
    without it the assistant would be unstartable on every non-developer host.
    """
    monkeypatch.setattr(assistant, "_get_taskdef_paths", lambda _root: [])
    with started(config) as status:
        assert status.running is True


def test_a_host_whose_workers_fill_the_cap_refuses_the_assistant(
    platform_root, fake_lmer
):
    """The assistant's slot is reserved, not conjured (T75).

    ``max_concurrent_sessions`` counts workers and the assistant is not one of
    them — but workers already holding every slot are holding the one it wanted
    too, so the start is refused with the numbers and the setting to raise. The
    daemon starts the assistant before it serves anything, so in practice it is
    first; this refusal is for the manual start on a host that came up busy.
    """
    config = cfg.load({"lmer_bin": str(fake_lmer), "max_concurrent_sessions": 1})
    registry.register("s-worker", kind="worker", pid=os.getpid())

    with pytest.raises(assistant.AssistantCapacityError) as caught:
        assistant.start(config)
    assert caught.value.status == 429
    assert "max_concurrent_sessions (1)" in str(caught.value)
    assert "1/1" in str(caught.value), "the numbers belong in the refusal"
    assert "Free a worker slot" in str(caught.value), "name what to free"
    assert events_of("assistant_start_refused")[-1]["data"]["reason"] == "cap_reached"


def test_a_live_assistant_leaves_every_worker_slot_free(platform_root, fake_lmer,
                                                        long_lived):
    """The other half, from this module's side: it spends none of the cap.

    Pinned here as well as in tests/test_platform_spawn.py because this is the
    module whose docstring used to argue the opposite, and because the assistant
    the daemon starts at boot is the entry that made the old arithmetic bite.
    """
    config = cfg.load({"lmer_bin": str(fake_lmer), "max_concurrent_sessions": 1})
    with started(config) as status:
        assert status.running is True
        worker = spawn.spawn_session(
            config,
            spawn.SpawnRequest(taskdef="develop", target="https://example.com/x"),
        )
        try:
            assert worker.session_id, "the chat window took the host's only slot"
        finally:
            kill(worker.pid)


def test_a_capacity_refusal_leaves_the_state_untouched(platform_root, fake_lmer):
    config = cfg.load({"lmer_bin": str(fake_lmer), "max_concurrent_sessions": 1})
    registry.register("s-worker", kind="worker", pid=os.getpid())
    with pytest.raises(assistant.AssistantCapacityError):
        assistant.start(config)
    assert assistant.read_state().generation == 0


def test_a_dead_worker_does_not_block_the_assistant(platform_root, fake_lmer,
                                                    long_lived):
    config = cfg.load({"lmer_bin": str(fake_lmer), "max_concurrent_sessions": 1})
    registry.register("s-dead", kind="worker", pid=2**22)
    with started(config) as status:
        assert status.running is True


def test_refusals_carry_an_http_status(platform_root):
    """Routes map ``.status`` off the exception, as in session_io and ask."""
    assert issubclass(assistant.AssistantAlreadyRunning, assistant.AssistantError)
    assert issubclass(assistant.AssistantCapacityError, assistant.AssistantError)
    assert issubclass(assistant.TaskdefMissing, assistant.AssistantError)
    assert assistant.AssistantError.status == 400


# --- a URL a container can dial (T30) ---------------------------------------
#
# `PlatformConfig.base_url` is where the daemon LISTENS, which is a different
# question from where a container can reach it, and the two differ for both
# configurations that occur in practice: the default bind is 127.0.0.1, which
# inside a container is the container's own loopback, and the usual alternative
# is 0.0.0.0, which is not a destination at all.
#
# The runtime is pinned in every one of these. The answer genuinely depends on
# it, and what a CI host happens to have installed is not something a test may
# quietly depend on — this host has docker and no podman, so an unpinned test
# here would assert the *unreachable* branch and look like it was passing.

def pin_runtime(monkeypatch, name):
    """Fix which container runtime ``container_base_url`` believes is here."""
    monkeypatch.setattr(cfg, "detect_runtime", lambda: name)


def forbid_runtime_detection(monkeypatch):
    """Fail the test if the runtime is consulted at all.

    Two rules answer before detection can matter — an operator's override, and a
    bind address a container can already route to — and "it happened to give the
    right URL anyway" is not the property either one is claiming.
    """
    monkeypatch.setattr(
        cfg, "detect_runtime",
        lambda: pytest.fail("the runtime must not be consulted for this case"),
    )


def pin_gateway(monkeypatch, address):
    """Fix what docker's default bridge gateway probe finds. ``None`` = nothing.

    Injected in every docker case, and not only because CI need not have docker:
    the probe shells out, and a test that let it run would assert whatever the
    host it happens to run on has.
    """
    monkeypatch.setattr(cfg, "_docker_bridge_gateway", lambda **_k: address)


def forbid_gateway_probe(monkeypatch):
    """Fail the test if the bridge gateway is probed at all.

    An override answers before any derivation, and a loopback bind is refused
    without one — in both cases a subprocess would be waste, and "it gave the
    right answer anyway" is not the property being claimed.
    """
    monkeypatch.setattr(
        cfg, "_docker_bridge_gateway",
        lambda **_k: pytest.fail("the bridge gateway must not be probed for this case"),
    )


HOST_ALIAS_URL = f"http://{cfg.PODMAN_HOST_ALIAS}:{cfg.DEFAULT_BIND_PORT}"


@pytest.mark.parametrize("address, kind", [
    ("127.0.0.1", "loopback"),
    ("127.0.0.53", "loopback"),
    ("::1", "loopback"),
    ("[::1]", "loopback"),
    ("localhost", "loopback"),
    ("LocalHost", "loopback"),
    ("0.0.0.0", "wildcard"),
    ("::", "wildcard"),
    ("*", "wildcard"),
    ("  ", "wildcard"),
    ("10.0.0.5", "routable"),
    ("2001:db8::1", "routable"),
    ("orchestrator.lan", "routable"),
])
def test_bind_addresses_are_classified_by_parsing_not_by_spelling(address, kind):
    """``is_loopback`` compares three literals; this decides what a session gets.

    ``127.0.0.53`` is the case that makes parsing rather than matching the right
    call — a real address on a systemd-resolved host, loopback, and not one of
    the three spellings the startup notice knows about.
    """
    assert cfg._address_kind(address) == kind


def test_a_routable_bind_is_handed_over_as_it_is(platform_root, monkeypatch):
    """On a LAN address the container is on the same network — no gateway needed."""
    forbid_runtime_detection(monkeypatch)
    reach = cfg.container_base_url(cfg.load({"bind_address": "10.0.0.5"}))
    assert reach.url == "http://10.0.0.5:8600"
    assert reach.source == "bind"
    assert reach.reason is None


def test_a_loopback_bind_becomes_podmans_host_alias(platform_root, monkeypatch):
    pin_runtime(monkeypatch, "podman")
    reach = cfg.container_base_url(cfg.load())
    assert reach.url == HOST_ALIAS_URL
    assert reach.source == "host-alias"


def test_a_wildcard_bind_is_never_handed_over_as_a_destination(
    platform_root, monkeypatch
):
    """0.0.0.0 is where a socket listens, not somewhere anything can connect."""
    pin_runtime(monkeypatch, "podman")
    reach = cfg.container_base_url(cfg.load({"bind_address": "0.0.0.0"}))
    assert reach.url == HOST_ALIAS_URL
    assert "0.0.0.0" not in reach.url


def test_a_loopback_bind_on_docker_is_still_refused(platform_root, monkeypatch):
    """The default bind, and the one case the bridge gateway cannot rescue.

    ``host.docker.internal`` does not resolve on Linux without ``--add-host`` and
    ``lmer_cli.runtime.base_run_args`` passes none, so there is no name for the
    host. Nor is there an address: the gateway reaches this host, and a socket on
    ``lo`` is not on it — so deriving one here would trade a stated reason for a
    connection refused, which is the error an operator hits first. The probe is
    not even run.
    """
    pin_runtime(monkeypatch, "docker")
    forbid_gateway_probe(monkeypatch)
    reach = cfg.container_base_url(cfg.load())
    assert reach.url is None
    assert reach.reachable is False
    assert "--add-host" in reach.reason
    assert "127.0.0.1" in reach.reason
    # Both ways out, named where the operator will read them.
    assert cfg.ENV_BIND_ADDRESS in reach.reason
    assert cfg.ENV_CONTAINER_URL in reach.reason


def test_a_wildcard_bind_on_docker_derives_the_bridge_gateway(
    platform_root, monkeypatch
):
    """The case an operator hits by default, and it needs no configuration.

    A socket on ``0.0.0.0`` is listening on the bridge gateway address too, so
    this is a route that exists rather than a name that might resolve: validated
    from a stock container, where the gateway answered 401 from ``/api/health``.
    """
    pin_runtime(monkeypatch, "docker")
    pin_gateway(monkeypatch, "172.17.0.1")
    reach = cfg.container_base_url(cfg.load({"bind_address": "0.0.0.0"}))
    assert reach.url == f"http://172.17.0.1:{cfg.DEFAULT_BIND_PORT}"
    assert reach.source == "bridge-gateway"
    assert reach.reason is None


def test_the_derived_url_carries_the_bind_port_and_the_probed_address(
    platform_root, monkeypatch
):
    """Neither half is a constant: the port is the operator's, the address the daemon's."""
    pin_runtime(monkeypatch, "docker")
    pin_gateway(monkeypatch, "10.200.0.1")
    reach = cfg.container_base_url(
        cfg.load({"bind_address": "::", "bind_port": 8180})
    )
    assert reach.url == "http://10.200.0.1:8180"


def test_a_gateway_that_cannot_be_found_falls_through_to_the_honest_reason(
    platform_root, monkeypatch
):
    """A derivation must never produce a URL it has no evidence for.

    And the reason has to name what actually failed: after this rule, "docker
    resolves no host-gateway name" would send the operator after the wrong thing
    on a wildcard bind — the bind is fine, the probe was not.
    """
    pin_runtime(monkeypatch, "docker")
    pin_gateway(monkeypatch, None)
    reach = cfg.container_base_url(
        cfg.load({"bind_address": "0.0.0.0", "bind_port": 8180})
    )
    assert reach.url is None
    assert reach.reachable is False
    assert "could not be determined" in reach.reason
    assert "--add-host" not in reach.reason, (
        "the missing host-gateway name is not what went wrong here"
    )
    # The port is in the reason because a reader may be able to find an address
    # this host could not — taskdef/orchestrate/instructions.txt tells it how.
    assert "8180" in reach.reason
    assert cfg.ENV_BIND_ADDRESS in reach.reason
    assert cfg.ENV_CONTAINER_URL in reach.reason


def test_the_docker_rule_leaves_podman_alone(platform_root, monkeypatch):
    """Podman had an answer for a wildcard bind already, and keeps it."""
    pin_runtime(monkeypatch, "podman")
    forbid_gateway_probe(monkeypatch)
    reach = cfg.container_base_url(cfg.load({"bind_address": "0.0.0.0"}))
    assert reach.url == HOST_ALIAS_URL
    assert reach.source == "host-alias"


def test_a_derivation_never_borrows_the_vocabulary_of_a_setting(
    platform_root, monkeypatch
):
    """``source`` is read by the endpoint log line and by an operator asking why.

    ``override`` is the one value that means a human set this. A URL the code
    worked out must not be reported in those terms — nothing downstream may tell
    an operator they configured something they did not.
    """
    pin_runtime(monkeypatch, "docker")
    pin_gateway(monkeypatch, "172.17.0.1")
    derived = cfg.container_base_url(cfg.load({"bind_address": "0.0.0.0"}))
    assert derived.source not in ("override", "bind")

    monkeypatch.setenv(cfg.ENV_CONTAINER_URL, "http://10.0.0.5:8600")
    assert cfg.container_base_url(cfg.load()).source == "override"


def test_a_host_with_no_container_runtime_says_so(platform_root, monkeypatch):
    def undetectable():
        raise cfg.RuntimeErrorDetect("Neither Docker nor Podman found in PATH")

    monkeypatch.setattr(cfg, "detect_runtime", undetectable)
    reach = cfg.container_base_url(cfg.load())
    assert reach.url is None
    assert "no container runtime could be detected" in reach.reason


def test_an_operator_override_beats_every_derivation(platform_root, monkeypatch):
    """The escape hatch for a proxy, a custom network, or a runtime we do not know."""
    forbid_runtime_detection(monkeypatch)
    monkeypatch.setenv(cfg.ENV_CONTAINER_URL, "https://orchestrator.internal:9443/")
    reach = cfg.container_base_url(cfg.load())
    assert reach.url == "https://orchestrator.internal:9443"
    assert reach.source == "override"


def test_an_override_beats_the_bridge_gateway_too(platform_root, monkeypatch):
    """Rule 1 stays rule 1 with the docker rule in front of it.

    An operator behind a proxy, or on a custom network, wrote something this
    could derive around — and a derivation quietly winning would make the escape
    hatch untrustworthy exactly where it is needed.
    """
    pin_runtime(monkeypatch, "docker")
    forbid_gateway_probe(monkeypatch)
    monkeypatch.setenv(cfg.ENV_CONTAINER_URL, "http://10.0.0.5:9000")
    reach = cfg.container_base_url(cfg.load({"bind_address": "0.0.0.0"}))
    assert reach.url == "http://10.0.0.5:9000"
    assert reach.source == "override"


@pytest.mark.parametrize("override, complaint", [
    ("http://0.0.0.0:8600", "wildcard"),
    ("http://[::]:8600", "wildcard"),
    ("host.containers.internal:8600", "not a URL"),
    ("/api", "not a URL"),
])
def test_an_override_that_cannot_work_anywhere_is_refused(
    platform_root, monkeypatch, override, complaint
):
    """Only the shapes that fail under every runtime; the rest is the operator's call."""
    pin_runtime(monkeypatch, "podman")
    monkeypatch.setenv(cfg.ENV_CONTAINER_URL, override)
    reach = cfg.container_base_url(cfg.load())
    assert reach.url is None
    assert complaint in reach.reason


def test_a_loopback_override_is_left_alone(platform_root, monkeypatch):
    """Wrong under every runtime lmer starts — and still the operator's to make.

    Second-guessing an explicit setting is how an escape hatch stops being one:
    a container in the host's own network namespace would correctly be told this.
    """
    pin_runtime(monkeypatch, "docker")
    monkeypatch.setenv(cfg.ENV_CONTAINER_URL, "http://127.0.0.1:8600")
    assert cfg.container_base_url(cfg.load()).url == "http://127.0.0.1:8600"


# --- what the session is actually given -------------------------------------

def env_file_values():
    """What ``lmer`` reads out of the ``--env-file`` the spawn named.

    Through ``dotenv_values`` rather than a hand-rolled split, because that is
    the parser on the other side of this file (``lmer_cli.cli`` merges its
    output into the container's environment) — so quoting that this reads but
    dotenv would not is a bug this helper hides.
    """
    return dict(dotenv_values(str(assistant.env_file_path())))


def command_line(session_id):
    """The argv the stub was launched with, echoed into its PTY log."""
    log = spawn.log_path_for(session_id)
    assert wait_for(
        lambda: log.is_file() and "fake lmer started:" in log.read_text(
            encoding="utf-8")
    ), f"the stub never announced itself in {log}"
    return log.read_text(encoding="utf-8")


def test_the_assistant_is_given_the_url_and_the_secret(
    config, long_lived, monkeypatch
):
    pin_runtime(monkeypatch, "podman")
    secret = cfg.ensure_secret(config)

    with started(config):
        values = env_file_values()
        assert values[assistant.ENV_PLATFORM_URL] == HOST_ALIAS_URL
        assert values[assistant.ENV_PLATFORM_CREDENTIAL] == secret
        assert assistant.ENV_PLATFORM_UNREACHABLE not in values


def test_a_docker_host_with_a_wildcard_bind_needs_no_configuration(
    platform_root, fake_lmer, long_lived, monkeypatch
):
    """The whole point of the docker rule, at the call site that spends it.

    ``_prepare_environment`` is where the derivation becomes a variable the
    session can read, and it runs once per start — so the probe it triggers is
    one subprocess per assistant, not per tick.
    """
    pin_runtime(monkeypatch, "docker")
    pin_gateway(monkeypatch, "172.17.0.1")
    config = cfg.load({"lmer_bin": str(fake_lmer), "bind_address": "0.0.0.0"})
    secret = cfg.ensure_secret(config)

    with started(config):
        values = env_file_values()
        assert values[assistant.ENV_PLATFORM_URL] == (
            f"http://172.17.0.1:{cfg.DEFAULT_BIND_PORT}"
        )
        assert values[assistant.ENV_PLATFORM_CREDENTIAL] == secret
        assert assistant.ENV_PLATFORM_UNREACHABLE not in values


def test_lmer_would_read_the_file_the_spawn_named(config, long_lived, monkeypatch):
    """The two halves have to meet: the flag, and a file lmer's own reader accepts.

    Asserting on the file's *content* alone would pass with a spawn that never
    passed ``--env-file``, and asserting on the flag alone would pass with a file
    dotenv cannot parse. This drives lmer's actual entry point for the flag.
    """
    pin_runtime(monkeypatch, "podman")
    cfg.ensure_secret(config)

    with started(config) as status:
        assert f"--env-file {assistant.env_file_path()}" in command_line(
            status.session_id
        )
        try:
            sources = cli.apply_env_file_defaults(
                [("--env-file", assistant.env_file_path())]
            )
            assert os.environ[assistant.ENV_PLATFORM_URL] == HOST_ALIAS_URL
            assert os.environ[assistant.ENV_PLATFORM_CREDENTIAL]
            assert assistant.ENV_PLATFORM_URL in sources
        finally:
            # apply_env_file_defaults writes os.environ directly, so monkeypatch
            # cannot undo it and a leftover would follow the suite out of here.
            for name in (assistant.ENV_PLATFORM_URL, assistant.ENV_PLATFORM_CREDENTIAL):
                os.environ.pop(name, None)


def test_the_secret_reaches_the_session_and_nothing_else(
    config, long_lived, monkeypatch
):
    """A file rather than a flag, because argv is echoed in three public places.

    ``ps``, the command list ``POST /api/sessions`` returns, and ``events.jsonl``
    — the same three ``spawn._build_command`` refuses to put a control token in.
    """
    pin_runtime(monkeypatch, "podman")
    secret = cfg.ensure_secret(config)

    with started(config) as status:
        assert secret not in command_line(status.session_id)
        assert secret not in store.events_path().read_text(encoding="utf-8")
        assert secret not in assistant.state_path().read_text(encoding="utf-8")
        assert secret not in json.dumps(registry.read_session(status.session_id))


def test_the_env_file_is_owner_only(config, long_lived, monkeypatch):
    """It is a second copy of the shared secret; it gets the secret's mode."""
    pin_runtime(monkeypatch, "podman")
    cfg.ensure_secret(config)

    with started(config):
        mode = stat.S_IMODE(assistant.env_file_path().stat().st_mode)
        assert mode == 0o600, f"env file mode is {mode:o}"


def test_the_env_file_is_created_owner_only_rather_than_corrected(
    config, long_lived, monkeypatch
):
    """A credential world-readable for a millisecond is a credential that leaked.

    The ``chmod`` behind the ``open`` is there for the file that already exists
    (``os.open`` ignores its mode for one), so it cannot be what protects a fresh
    one — with it disabled the mode still has to come out right. Without this
    the two lines cover for each other and the creation mode can be widened
    without any test noticing.

    The umask is pinned for the length of the check: it can only *remove* bits,
    so a strict one on the host would mask a wide ``open()`` from this assertion.
    """
    pin_runtime(monkeypatch, "podman")
    cfg.ensure_secret(config)
    monkeypatch.setattr(os, "chmod", lambda *args, **kwargs: None)

    previous = os.umask(0o022)
    try:
        with started(config):
            mode = stat.S_IMODE(assistant.env_file_path().stat().st_mode)
    finally:
        os.umask(previous)
    assert mode == 0o600, f"the env file was created {mode:o} by os.open"


def test_the_env_file_lands_in_an_owner_only_directory(config, monkeypatch):
    """0600 in a 0755 directory still publishes the filename and the mtime (T93).

    Called directly rather than through a start, because a start writes state and
    history afterwards and the store would tighten the directory on its way past —
    the question here is whether *this* write leaves the tree as it found it. The
    directory is deliberately wide beforehand: ``config.ensure_secret`` creates the
    same directory with its own umask, so a pre-existing 0755 is the normal case
    rather than a contrived one.
    """
    pin_runtime(monkeypatch, "podman")
    cfg.ensure_secret(config)
    directory = assistant.env_file_path().parent
    directory.chmod(0o755)

    path, _reach = assistant._prepare_environment(config)
    assert path is not None, "the env file did not write at all"
    mode = stat.S_IMODE(directory.stat().st_mode)
    assert mode == 0o700, f"the state dir is mode {mode:o}"


def test_a_pre_existing_permissive_env_file_is_tightened(
    config, long_lived, monkeypatch
):
    """``os.open`` ignores its mode for a file that already exists."""
    pin_runtime(monkeypatch, "podman")
    cfg.ensure_secret(config)
    path = assistant.env_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stale\n", encoding="utf-8")
    path.chmod(0o644)

    with started(config):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_history_records_where_it_was_told_to_look_but_not_the_key(
    config, long_lived, monkeypatch
):
    pin_runtime(monkeypatch, "podman")
    cfg.ensure_secret(config)

    with started(config):
        data = events_of("assistant_started")[-1]["data"]
        assert data["platform_url"] == HOST_ALIAS_URL
        assert assistant.ENV_PLATFORM_CREDENTIAL.lower() not in json.dumps(data).lower()


def test_an_unreachable_platform_is_explained_instead_of_faked(
    config, long_lived, monkeypatch
):
    """The whole point of not deriving a URL blindly: the session is told why."""
    pin_runtime(monkeypatch, "docker")
    cfg.ensure_secret(config)

    with started(config):
        values = env_file_values()
        assert assistant.ENV_PLATFORM_URL not in values
        assert assistant.ENV_PLATFORM_CREDENTIAL not in values, (
            "a credential with nothing to spend it on is still a credential"
        )
        assert "--add-host" in values[assistant.ENV_PLATFORM_UNREACHABLE]


def test_a_host_with_no_secret_yet_gets_no_url_either(
    config, long_lived, monkeypatch
):
    """A URL with no credential is a 401 machine; say what is missing instead."""
    pin_runtime(monkeypatch, "podman")
    assert cfg.read_secret(config) is None

    with started(config):
        values = env_file_values()
        assert assistant.ENV_PLATFORM_URL not in values
        assert "no shared secret" in values[assistant.ENV_PLATFORM_UNREACHABLE]


def test_an_unreadable_secret_does_not_stop_the_assistant(
    config, long_lived, monkeypatch, caplog
):
    pin_runtime(monkeypatch, "podman")
    cfg.ensure_secret(config)
    monkeypatch.setattr(
        assistant, "read_secret",
        lambda *_a, **_k: (_ for _ in ()).throw(cfg.ConfigError("permission denied")),
    )

    with started(config) as status:
        assert status.running is True
        assert assistant.ENV_PLATFORM_URL not in env_file_values()
    assert any(
        "platform_assistant_secret_unreadable" in r.message for r in caplog.records
    )


def test_an_unwritable_env_file_costs_the_credentials_not_the_chat(
    config, long_lived, monkeypatch, tmp_path, caplog
):
    """The registry is what makes a session real (as in ``_write_state``).

    Refusing to open the operator's chat window because a bookkeeping file would
    not write trades a small problem for a total one.
    """
    pin_runtime(monkeypatch, "podman")
    cfg.ensure_secret(config)
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("", encoding="utf-8")
    monkeypatch.setattr(assistant, "env_file_path", lambda: blocked / "assistant.env")

    with started(config) as status:
        assert status.running is True
        assert "--env-file" not in command_line(status.session_id), (
            "a flag naming a file that was never written is worse than no flag"
        )
    assert any(
        "platform_assistant_env_unwritable" in r.message for r in caplog.records
    )


# --- the other half: instructions for the API it was handed -----------------

ORCHESTRATE_ROUTES = (
    "/api/state",
    "/api/sessions",
    "/api/sessions/{id}/input",
    "/api/sessions/{id}/messages",
    "/api/sessions/{id}/ask/{qid}/answer",
    "/api/runs/answer",
    # Named for the same reason as the three below: since T88 the instructions tell
    # the assistant to name every run it starts, and the verb that does it
    # afterwards is half of that instruction.
    "/api/runs/meta",
    "/api/runs/relations",
    "/api/runs/relate",
    "/api/runs/unrelate",
    "/api/sessions/{id}/wind-down",
    "/api/sessions/{id}/exit",
    # The assistant's own state. Named here rather than left to ``GET /api`` — as
    # the handoff route still is — because the instructions the taskdef gives are
    # *about* these three: follow the standing orders, watch the pending count,
    # take the spool. An instruction whose subject the agent has to go and find is
    # one it can follow half of.
    "/api/assistant",
    "/api/assistant/instructions",
    "/api/assistant/pending",
)


def orchestrate_instructions():
    return (assistant.taskdef_dir() / "instructions.txt").read_text(encoding="utf-8")


def orchestrate_prose():
    """The same text as one line, for assertions about what it *says*.

    The file is hand-wrapped at 79 columns, so a phrase that fits on one line today
    straddles two after an edit three words earlier — and a test that failed for
    that is a test about where a paragraph wraps. Route names and code fragments are
    single tokens and read the same either way.
    """
    return " ".join(orchestrate_instructions().split())


def test_the_taskdef_documents_the_api_behind_the_cli(platform_root):
    """A URL and a key are no use without knowing what to ask for.

    Spec §8.2's ``lmer-ctl`` was dropped and then asked for again (T102), which
    changes which of the two is *preferred* here and nothing about what has to be
    written down. The routes stay: the CLI ships with this file and the daemon may
    be newer than both, so a verb it lacks has to be a route the assistant can
    still call rather than a thing the fleet cannot do.

    Including the one distinction the codebase repeats everywhere — answering a
    *live* session and answering a *stopped run* are different verbs — which now
    has to be true of the two CLI verbs as well.
    """
    text = orchestrate_instructions()
    assert "lmer-ctl" in text, "the CLI exists to be reached for; nothing says so"
    for name in (assistant.ENV_PLATFORM_URL, assistant.ENV_PLATFORM_CREDENTIAL,
                 assistant.ENV_PLATFORM_UNREACHABLE):
        assert name in text, f"{name} is the contract and is not mentioned"
    for route in ORCHESTRATE_ROUTES:
        assert route in text, f"{route} is not documented"
    assert "GET /api" in text, "the served route list has to be named as the authority"


def test_the_taskdef_prefers_the_cli_without_dropping_the_routes(platform_root):
    """T102. The reasoning that dropped the CLI — the daemon enforces everything
    and the API is the surface — is still true, so teaching the CLI as the way in
    has to carry both halves: reach for it, and fall back to the routes.

    The two verbs that spawn a container are named through the CLI as well, because
    the mistake the API index calls out (answering a stopped run at a live session's
    route) is available at the command line too.
    """
    text = orchestrate_prose()
    assert "Reach for `lmer-ctl` first" in text, (
        "nothing says which of the two paths to use, so both are equally advised"
    )
    assert "It decides nothing" in text, (
        "the CLI reads as a second enforcer unless it is said that it is not"
    )
    assert "`curl` stays correct and is the fallback" in text, (
        "an agent told to prefer a CLI reports a missing verb as a broken fleet "
        "unless the fallback is spelled out"
    )
    for verb in ("lmer-ctl status", "lmer-ctl answer", "lmer-ctl runs answer",
                 "lmer-ctl spawn", "lmer-ctl send"):
        assert verb in text, f"{verb!r} is not taught"
    # The credential never reaches argv, which is the CLI's own reason to exist
    # next to `curl` — and the taskdef's rule about not echoing it.
    assert "no credential of yours ever reaches a command line" in text


def test_the_taskdef_teaches_both_halves_of_the_standing_orders(platform_root):
    """T87. The operator's ask was "tell him via the chat, not some ux config
    thing", so both halves are prompt-side and both have to be here.

    Reading them is worth little without keeping them current, and keeping them
    current is dangerous without the three rules that make a full-document write
    safe: confirm the wording (they cannot see the file), re-read before writing
    (two incarnations must not clobber), and keep it rule-shaped (every future
    incarnation pays to read it).
    """
    text = orchestrate_prose()
    assert "/api/assistant/instructions" in text
    assert "standing" in text.lower(), "the document is never named as what it is"
    # Read at startup, and *followed* — not merely fetched.
    assert "at startup" in text
    assert "follow it" in text.lower()
    # The write path, and the phrases that trigger it.
    for trigger in ("from now on", "always", "stop doing"):
        assert trigger in text, f"the standing-preference trigger {trigger!r} is absent"
    assert "Confirm the wording back" in text
    assert "Re-read the document" in text, (
        "nothing tells it to re-read before a whole-document write, so two "
        "incarnations can clobber each other"
    )
    assert "there is no append" in text, (
        "a POST that replaces everything reads as an append unless it is said"
    )
    assert "not a diary" in text, "nothing keeps the document short and rule-shaped"


def test_the_taskdef_corrects_the_confabulation_that_something_pushes(platform_root):
    """The failure this text exists to prevent, observed live.

    A running incarnation told the operator "the orchestrator already pushes me
    digests" and then sat idle while a finished review's digest waited in the
    spool until it was evicted. The old wording ("hands you a short digest")
    invited exactly that reading, so the correction has to be explicit: nothing
    arrives, and being idle means being deaf.
    """
    text = orchestrate_prose()
    assert "does not push digests to you" in text, (
        "the taskdef never states plainly that nothing pushes"
    )
    assert "spool" in text
    for fact in (
        "No message arrives in this session",
        "Nothing interrupts you",
        "you are deaf",
    ):
        assert fact in text, f"the consequence {fact!r} is left to be inferred"
    # And the old invitation to the wrong belief is gone.
    assert "hands you a short digest" not in text, (
        "the wording the confabulation came from is still here"
    )


def test_the_taskdef_teaches_the_watch_and_its_failure_modes(platform_root):
    """T89. An idle LLM session cannot poll, so the only way it hears anything is a
    watch it arms itself — and every way that goes wrong is a way it goes silently
    deaf again.

    The re-arm is the one that matters most and is stated at the *take* site rather
    than only in a startup checklist: an incarnation that armed one watch at
    startup and then took the spool once has already stopped listening.
    """
    text = orchestrate_prose()
    assert "Monitor" in text, "the harness's watch tool is not named"
    # What it watches, and the two verbs kept apart: the count is polled, the take
    # is called on wake and never polled.
    assert ".pending > 0" in text, "the until-condition is not spelled out"
    assert "/api/assistant/pending" in text
    assert "30 seconds" in text, (
        "no polling interval, so it will poll as fast as it can"
    )
    assert "an hour" in text, "the watch is uncapped"
    assert "Arm one at startup" in text
    assert "arm the next watch" in text, (
        "re-arming is not stated where the spool is taken, so one take makes it deaf"
    )
    assert "part of taking the spool, every single time" in text, (
        "re-arming reads as a startup step rather than a standing duty"
    )
    assert "One watch at a time" in text
    assert "before you arm" in text, "nothing says to check for an existing watch"
    assert "twice, stop" in text and "Tell the operator" in text, (
        "a watch that keeps erroring is looped instead of reported"
    )
    assert "The wake carries no digest" in text, (
        "the wake looks like it might carry the news, which would make the spool "
        "optional to read"
    )


def test_the_taskdef_teaches_the_assistant_to_name_the_runs_it_starts(platform_root):
    """T88. The operator reads a list on a phone, and an untitled run is one they
    have to identify from a slug and a taskdef — so the ask was that uber lmer name
    a run when it spawns one, not that a field merely exist.

    Both ways in have to be here, because they are not interchangeable: the spawn
    field is one call and cannot be half-done, while the metadata verb is the only
    way left once the reply has been read. So the field is named in the spawn
    route's own shape as well as in the prose, and the run key the reply carries is
    spelled out — an agent that has to guess how to address the run it just started
    will fall back to leaving it untitled.
    """
    text = orchestrate_prose()
    assert "Name every run you start" in text, (
        "nothing tells the assistant to name a run at all, which is the ask"
    )
    assert "{taskdef,target,title,...}" in text, (
        "the spawn route's shape does not show the title, so the atomic path is "
        "invisible to an agent reading the route list"
    )
    assert "POST /api/runs/meta" in text, "the way to name a run afterwards is absent"
    assert "run:{host,project,slug}" in text, (
        "the reply's run key is not spelled out, so an agent that spawned first has "
        "to guess how to address the run"
    )
    assert "untitled" in text, "why any of this matters is left to be inferred"


@pytest.mark.parametrize("environment, reachable", [
    ({"LMER_PLATFORM_URL": "http://host.containers.internal:8600"}, True),
    ({"LMER_PLATFORM_UNREACHABLE": "bound to 127.0.0.1 and the runtime is docker"},
     False),
    # The env file could not be written at all: no URL and no reason either.
    ({}, False),
])
def test_the_taskdef_only_advertises_an_api_it_was_actually_given(
    platform_root, monkeypatch, environment, reachable
):
    """Rendered, not read: the conditional is the thing being checked.

    An agent told to ``curl "$LMER_PLATFORM_URL/api/state"`` with that variable
    unset does not report a misconfiguration, it reports a broken fleet.

    The claim is about the *fleet* routes, all of which need the bearer token an
    unreachable session was never given. ``/api/health`` is the documented
    exception and is named in the unreachable branch on purpose: it answers 401
    without a credential, which is what makes it a probe rather than a route.
    """
    from hooks.start import render_taskdef_template

    monkeypatch.setenv("LMER_TASKDEF_ROOT", str(assistant.taskdef_dir().parent))
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    rendered = render_taskdef_template(
        assistant.taskdef_dir() / "instructions.txt",
        extra_context={"instructions_file": "/x"},
    )
    for route in ORCHESTRATE_ROUTES:
        assert (route in rendered) is reachable, (
            f"{route} must not be shown to a session that cannot call it — an "
            "unreachable session gets no route list at all, not a shorter one"
        )
    assert ("cannot reach the platform" in rendered) is not reachable
    assert ("$LMER_PLATFORM_SECRET" in rendered) is reachable
    assert ("lmer-ctl" in rendered) is reachable, (
        "the CLI reads the same two variables, so a session that was given "
        "neither has no more use for it than for the routes"
    )


def render_unreachable_taskdef(monkeypatch, reason):
    """The orchestrate instructions as a session with no platform URL reads them."""
    from hooks.start import render_taskdef_template

    monkeypatch.setenv("LMER_TASKDEF_ROOT", str(assistant.taskdef_dir().parent))
    monkeypatch.setenv(assistant.ENV_PLATFORM_UNREACHABLE, reason)
    return render_taskdef_template(
        assistant.taskdef_dir() / "instructions.txt",
        extra_context={"instructions_file": "/x"},
    )


def test_an_unreachable_session_is_taught_to_find_the_gateway_itself(
    platform_root, monkeypatch
):
    """The host's derivation can fail where the container's own read succeeds.

    ``/proc/net/route`` is inside the container and needs no docker CLI, no
    ``ip``, and no permission the session lacks — so on a wildcard bind the agent
    can often name the address the daemon could not, which turns "the fleet is
    broken" into one actionable sentence for the operator.
    """
    rendered = render_unreachable_taskdef(
        monkeypatch,
        cfg._unreachable_reason(
            cfg.load({"bind_address": "0.0.0.0", "bind_port": 8180}),
            "wildcard", "docker",
        ),
    )
    assert "/proc/net/route" in rendered
    assert "00000000" in rendered, "the default route's destination is how it is found"
    assert "little-endian" in rendered, "the column is hex, and reversed"
    assert "/api/health" in rendered
    assert "401" in rendered, "a 401 is the found signal — the route exists"
    # It ends as a report, because the secret cannot be self-served.
    assert cfg.ENV_CONTAINER_URL in rendered
    assert "8180" in rendered, "the port comes from the reason, not from this file"


def test_the_probe_is_bounded_and_does_not_become_a_retry_loop(
    platform_root, monkeypatch
):
    """The one guidance this must not undo: do not guess at a URL and retry.

    A probe and a loop are different things, and the fragment has to say which
    this is — an agent that reads "find the gateway" as "keep trying the API"
    would burn its context on connection errors instead of reporting.
    """
    rendered = render_unreachable_taskdef(monkeypatch, "bound to '0.0.0.0' port 8180")
    assert "do not retry" in rendered, "the original bound survives the addition"
    for bound in ("exactly once", "one probe", "never run the probe a second time"):
        assert bound in rendered, f"the probe is not bounded: {bound!r} is missing"


def test_a_second_start_does_not_rewrite_what_the_live_one_was_given(
    config, long_lived, monkeypatch
):
    """The reachability answer can change under a running assistant.

    An operator rebinding the platform, or installing podman, changes what this
    would derive — and the live session read its file at launch. Rewriting it
    behind a refused start would leave the file disagreeing with the session it
    describes, for no gain: nothing re-reads it.
    """
    pin_runtime(monkeypatch, "podman")
    cfg.ensure_secret(config)

    with started(config):
        pin_runtime(monkeypatch, "docker")
        with pytest.raises(assistant.AssistantAlreadyRunning):
            assistant.start(config)
        assert env_file_values()[assistant.ENV_PLATFORM_URL] == HOST_ALIAS_URL


def test_a_rotation_rewrites_the_environment_for_the_new_incarnation(
    config, long_lived, monkeypatch
):
    """A rotation is a stop and a start, so the successor gets its own file."""
    pin_runtime(monkeypatch, "docker")
    cfg.ensure_secret(config)

    with started(config):
        assert assistant.ENV_PLATFORM_URL not in env_file_values()
        pin_runtime(monkeypatch, "podman")
        rotated = assistant.rotate(config)
        try:
            assert env_file_values()[assistant.ENV_PLATFORM_URL] == HOST_ALIAS_URL
        finally:
            kill(rotated.pid)
