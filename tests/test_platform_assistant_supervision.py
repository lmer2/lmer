"""The daemon auto-starts and supervises the assistant (issue #141, T63; spec §8.1).

Spec §8.1 says the assistant is "long-lived (D11), supervised by the daemon
(respawned if it dies)". :func:`lmer_platform.assistant.ensure_running` has
existed since T29 and nothing called it, so an operator had to
``POST /api/assistant/start`` by hand on every boot. This is the wiring, and the
five ways it goes badly wrong if it is wired carelessly:

1. a start that fails must not stop the daemon serving — the fleet view is the
   thing an operator needs when something is broken;
2. the respawn backs off and gives up out loud, because every attempt is a clone
   and an image pull;
3. the assistant holds its own slot rather than one of the configured
   ``max_concurrent_sessions`` (T75), and the startup notice says so — an operator
   who learned the old arithmetic would otherwise keep planning around a number
   that is no longer short by one;
4. a live assistant's 0600 environment file is never rewritten under it — the
   auto-start runs on every boot, so this path is now the common one;
5. an unreachable platform still gets an assistant, and the daemon says so where
   an operator will read it.

Sessions are started for real against the same stub ``lmer``
``tests/test_platform_assistant.py`` uses, so the spawn, the registry entry and
the liveness checks are genuinely exercised without launching a container. Two
conventions from that module carry over and are load-bearing here:

- a stub that exits cleanly has its registry entry reaped by the watcher thread,
  so anything asserting about a *running* assistant sets ``FAKE_LMER_SLEEP`` and
  kills the process afterwards;
- an exit is awaited through :func:`lmer_platform.spawn.wait_for_exit_recorded`,
  never by re-reading the registry in a loop — that is a race against a thread,
  not a check.

Nothing here sleeps and hopes. The clock, the backoff wait and "how a death is
observed" are all constructor parameters of
:class:`lmer_platform.assistant.Supervisor`, so a crash loop is driven by a hand
turned clock and asserted on the delays that *would* have been waited.
"""

import contextlib
import os
import sys
import threading
import time

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient

from lmer_platform import api, assistant, daemon, registry, spawn, store
from lmer_platform import config as cfg
from tests.conftest import strip_lmer_env

SECRET = "test-secret-value"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in ("LMER_REPO_URL", "LMER_PLATFORM_PORTS_FILE", "FAKE_LMER_SLEEP",
                 "FAKE_LMER_EXIT", cfg.ENV_CONTAINER_URL, cfg.ENV_SECRET_FILE):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_stray_supervisor(monkeypatch):
    """Each test starts with nothing supervising, and leaves nothing behind.

    The re-arm seam (T75) is process-wide state, like ``reattach``'s drains: a
    supervisor a test registered would otherwise answer for the *next* test's
    manual start and spawn a container into it. Set through ``monkeypatch`` in both
    directions so the teardown is not a thing a failing test can skip.
    """
    monkeypatch.setattr(assistant, "_SUPERVISOR", None)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture
def fake_lmer(tmp_path):
    """A stub standing in for `lmer`: announces itself, then exits.

    With ``FAKE_LMER_SLEEP`` it stays up instead, which is how a test keeps a
    registry entry around long enough to assert on it. Without it the stub is a
    session that dies the instant it starts — which is precisely the crash loop
    the backoff exists for, so both halves of this module use the same stub.
    """
    script = tmp_path / "fake-lmer"
    script.write_text(
        "#!/bin/sh\n"
        'echo "fake lmer started: $*"\n'
        'if [ -n "$FAKE_LMER_SLEEP" ]; then sleep "$FAKE_LMER_SLEEP"; fi\n'
        'exit "${FAKE_LMER_EXIT:-0}"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


@pytest.fixture
def config(platform_root, fake_lmer):
    return cfg.load({"lmer_bin": str(fake_lmer)})


@pytest.fixture
def long_lived(monkeypatch):
    """Keep the stub alive for the length of a test.

    A clean exit reaps the registry entry, and the registry is what answers "is
    an assistant running" — so without this every assertion about a live
    assistant races the watcher thread.
    """
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")


class FakeClock:
    """A monotonic clock the test advances by hand.

    The container this suite runs in has one CPU, so "wait and see whether the
    supervisor thinks the session was short-lived" is a flake. The threshold is
    read off this instead, and the test says how long the incarnation lived.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def kill(pid):
    if isinstance(pid, int) and pid > 1:
        with contextlib.suppress(OSError):
            os.kill(pid, 9)


def kill_live_assistant():
    """Stop whatever assistant is up, however many incarnations ago it started."""
    kill(assistant.status().pid)


def events_of(event_type):
    return [e for e in store.read_events() if e.get("type") == event_type]


def client_for(config):
    """A client over the real routes, with a stub fleet view (no work repo).

    The same one-liner ``tests/test_platform_assistant_routes.py`` uses. Needed
    here because the re-arm seam hangs off ``POST /api/assistant/start`` and
    calling the module function instead would prove the seam works while leaving
    the wiring — the only part that was missing — untested.
    """
    return TestClient(
        api.create_app(
            config, SECRET, state_builder=lambda config, force_pull=False: {}
        )
    )


def bearer_header():
    return {"Authorization": f"Bearer {SECRET}"}


def wait_for_replacement(previous_id, timeout=20.0):
    """The incarnation that replaced *previous_id*, or the status when time ran out.

    A bounded wait on a *fact* rather than an assertion about a duration: the
    respawn happens on the supervisor's own thread, and this container has one CPU,
    so the only honest thing to wait for is the registry saying a different session
    is up. Registry replacement and generation are now one publication, so the
    first replacement status is already complete. Nothing here asserts how long it
    took.
    """
    deadline = time.monotonic() + timeout
    while True:
        current = assistant.status()
        if current.running and current.session_id != previous_id:
            return current
        if time.monotonic() >= deadline:
            return current
        time.sleep(0.05)


def test_status_cannot_observe_a_spawn_before_its_generation_is_published(
    platform_root, fake_lmer, monkeypatch
):
    """Hold the exact registry/state publication gap from the CI failure.

    ``spawn_session`` registers the live child before ``assistant.start`` writes
    the state that names it and increments the generation. Under load a status
    reader landed between those writes and returned a real new session with the
    previous generation and ``tracked=False``. The supervision loop then made a
    decision from a status that never existed as a complete transition.

    No scheduler race in this test: the state write is held after the registry
    entry exists, and an observed lock announces when the reader has actually
    tried to enter. It must remain blocked until publication is released, then
    see the complete new pair.
    """
    config = cfg.load({"lmer_bin": str(fake_lmer)})
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")

    publication_reached = threading.Event()
    release_publication = threading.Event()
    status_waiting = threading.Event()
    status_done = threading.Event()
    real_write = assistant._write_state
    real_lock = assistant._PUBLICATION_LOCK
    started = {}
    observed = {}

    def held_write(state):
        publication_reached.set()
        assert release_publication.wait(timeout=10), "the publication was never released"
        return real_write(state)

    reader = None

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread() is reader:
                status_waiting.set()
            return real_lock.__enter__()

        def __exit__(self, *exc_info):
            return real_lock.__exit__(*exc_info)

    def launch():
        started["status"] = assistant.start(config)

    def read_status():
        observed["status"] = assistant.status()
        status_done.set()

    monkeypatch.setattr(assistant, "_write_state", held_write)
    monkeypatch.setattr(assistant, "_PUBLICATION_LOCK", ObservedLock())
    launcher = threading.Thread(target=launch, name="held-assistant-start")
    reader = threading.Thread(target=read_status, name="assistant-status-reader")
    try:
        launcher.start()
        assert publication_reached.wait(timeout=10), (
            "the spawn never reached the state-publication boundary"
        )
        # The live entry is already public; only its matching state is held.
        assert registry.list_sessions(live_only=True), "the registry publication is absent"

        reader.start()
        assert status_waiting.wait(timeout=10), "the status reader never reached the lock"
        assert status_done.is_set() is False, (
            "status crossed the publication gap and returned a torn registry/state pair"
        )

        release_publication.set()
        launcher.join(timeout=10)
        reader.join(timeout=10)
        assert not launcher.is_alive() and not reader.is_alive()

        current = observed["status"]
        assert current.running is True
        assert current.tracked is True
        assert current.session_id == started["status"].session_id
        assert current.generation == started["status"].generation == 1
    finally:
        release_publication.set()
        launcher.join(timeout=10)
        if reader.ident is not None:
            reader.join(timeout=10)
        kill_live_assistant()


def test_status_does_not_wait_for_the_container_spawn(
    platform_root, fake_lmer, monkeypatch
):
    """The transition lock may cover spawn; the publication lock must not."""
    config = cfg.load({"lmer_bin": str(fake_lmer)})
    spawn_held = threading.Event()
    release_spawn = threading.Event()
    status_done = threading.Event()
    started = {}

    def held_spawn(_config, _request, *, kind, publish_registration):
        spawn_held.set()
        assert release_spawn.wait(timeout=10), "the held spawn was never released"

        def register():
            registry.register("s-1", kind=kind, pid=os.getpid())

        publish_registration(register, "s-1", os.getpid())
        return _fake_spawn_result()

    def launch():
        started["status"] = assistant.start(config)

    def read_status():
        assistant.status()
        status_done.set()

    monkeypatch.setattr(assistant.spawn, "spawn_session", held_spawn)
    launcher = threading.Thread(target=launch)
    reader = threading.Thread(target=read_status)
    try:
        launcher.start()
        assert spawn_held.wait(timeout=10)
        reader.start()
        reader.join(timeout=1)
        assert status_done.is_set(), "status waited for the whole container spawn"
        release_spawn.set()
        launcher.join(timeout=10)
        assert started["status"].generation == 1
    finally:
        release_spawn.set()
        launcher.join(timeout=10)
        reader.join(timeout=10)
        registry.remove("s-1")


def pin_runtime(monkeypatch, name):
    """Fix which container runtime ``container_base_url`` believes is here.

    Pinned in every test that cares, as in ``tests/test_platform_assistant.py``:
    this host has docker and no podman, so an unpinned test asserting the
    reachable branch would quietly assert the unreachable one.
    """
    monkeypatch.setattr(cfg, "detect_runtime", lambda: name)


def serve_nothing(monkeypatch, on_serve=None):
    """Stand in for uvicorn so ``lmer platform run`` returns instead of serving.

    *on_serve* is called at the moment the server would start accepting, which is
    how "before uvicorn accepts anything" is asserted rather than assumed.
    """
    class FakeUvicorn:
        @staticmethod
        def run(app, **kwargs):
            if on_serve is not None:
                on_serve(kwargs)

    monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)


def save_config(**values):
    """Persist a config.json, since ``lmer platform run`` loads its own."""
    cfg.save(cfg.PlatformConfig(**values))


# --- the daemon starts it, and only `run` does -------------------------------

def test_run_starts_the_assistant_before_it_serves(
    platform_root, fake_lmer, long_lived, monkeypatch, capsys
):
    """The operator asked: 'it should definitely auto start it, its a core feature'.

    Asserted at the moment the server would accept its first request, because
    that is the property: an assistant that comes up afterwards is missing from
    the first fleet view anyone renders.
    """
    save_config(lmer_bin=str(fake_lmer))
    at_serve = {}
    serve_nothing(monkeypatch, lambda _kwargs: at_serve.update(
        running=assistant.status().running, session=assistant.status().session_id
    ))

    try:
        assert daemon.main(["run"]) == 0
        assert at_serve["running"] is True, (
            "the assistant must be up before uvicorn accepts anything"
        )
        out = capsys.readouterr().out
        assert f"Assistant started as {at_serve['session']}" in out
        assert "generation 1" in out
    finally:
        kill_live_assistant()


def _fake_spawn_result():
    from lmer_platform.spawn import SpawnResult

    return SpawnResult(
        session_id="s-1", pid=4242, log_path="/logs/s-1.log",
        host="gitlab.example.com", project="agents/global", slug="develop-1",
        command=["lmer", "develop", "t"],
    )


@pytest.mark.parametrize("argv", [
    ["status"], ["status", "--json"], ["rescan"], ["runs"], ["runs", "--candidates"],
    ["secret"], ["adopt", "gitlab.example.com/agents/global/x"],
    ["forget", "gitlab.example.com/agents/global/x"],
    ["spawn", "develop", "https://example.com/x"], ["setup-ui"],
])
def test_no_other_subcommand_launches_a_container(
    platform_root, fake_lmer, monkeypatch, argv
):
    """Every other verb is diagnostic or one-shot, often run several times over.

    ``lmer platform status`` starting a container as a side effect would make the
    command an operator reaches for *while* something is wrong the one they
    cannot use.
    """
    save_config(lmer_bin=str(fake_lmer))
    monkeypatch.setattr(
        daemon, "_supervise_assistant",
        lambda config: pytest.fail(
            f"`lmer platform {' '.join(argv)}` started the assistant"
        ),
    )
    # Neither of these may reach the real thing: one spawns, the other downloads
    # a Node toolchain.
    monkeypatch.setattr(daemon, "spawn_session", lambda c, r: _fake_spawn_result())
    monkeypatch.setattr(daemon, "setup_ui", lambda force_node=False: platform_root)

    daemon.main(argv)
    assert registry.list_sessions(live_only=True) == []


# --- (1) a failure to start must not stop the daemon serving -----------------

def test_a_refused_start_does_not_stop_the_daemon_serving(
    platform_root, fake_lmer, monkeypatch, capsys
):
    """A platform that boots and says the assistant is down beats one that
    refuses to boot: the fleet view is what an operator needs when something is
    broken, and it does not depend on the assistant at all."""
    save_config(lmer_bin=str(fake_lmer), max_concurrent_sessions=1)
    registry.register("s-worker", kind="worker", pid=os.getpid())
    served = []
    serve_nothing(monkeypatch, served.append)

    assert daemon.main(["run"]) == 0
    assert served, "the daemon must serve even when the assistant would not start"

    out = capsys.readouterr().out
    assert "Assistant NOT running" in out
    assert "POST /api/assistant/start" in out, "say how to get one by hand"
    assert assistant.status().running is False


def test_an_unexpected_failure_is_absorbed_and_reported(config, monkeypatch, caplog):
    """Not only refusals: a host with no ``lmer``, or an unwritable state dir.

    Same reasoning as ``reattach.reattach_all``'s blanket catch — one broken
    thing must not cost the daemon its startup.
    """
    monkeypatch.setattr(
        assistant, "ensure_running",
        lambda _config: (_ for _ in ()).throw(RuntimeError("no container runtime")),
    )
    supervisor = assistant.Supervisor(config)
    report = supervisor.first_start()

    assert report.running is False
    assert "RuntimeError: no container runtime" in report.error
    assert "Assistant NOT running" in report.notice
    assert supervisor.failures == 1, "a failure spends one unit of the budget"
    assert any(
        "platform_assistant_autostart_failed" in r.message for r in caplog.records
    )


# --- (2) backoff, and giving up out loud -------------------------------------

def test_a_crash_looping_assistant_backs_off_and_gives_up(config, caplog):
    """Every attempt is a clone and an image pull, so this cannot be a tight loop.

    The stub exits the instant it starts, which is exactly what a container that
    fails during launch looks like from here: the *start* succeeded every time. A
    supervisor that only counted failed starts would respawn this forever, which
    is why the budget is spent by an incarnation that did not stay up.

    Deterministic by construction: the clock never advances, so every incarnation
    reads as short-lived, and the delays are recorded rather than waited.
    """
    delays = []
    supervisor = assistant.Supervisor(
        config, clock=FakeClock(), sleep=delays.append, attempts=3, backoff=5.0,
        poll=2.0,
    )

    supervisor.run()

    assert supervisor.gave_up is True
    assert delays == [5.0, 10.0], "the wait doubles between attempts"
    assert assistant.read_state().generation == 3, (
        "three containers, then it stopped spending them"
    )
    assert events_of("assistant_supervision_gave_up"), (
        "giving up has to be in history, not only in a log nobody kept"
    )
    assert any(
        "platform_assistant_supervision_gave_up" in r.message for r in caplog.records
    )
    assert any(
        "platform_assistant_crash_loop" in r.message for r in caplog.records
    )


def test_the_backoff_doubles_and_then_stops_growing(config):
    """A doubling with no ceiling stops retrying in any useful sense by hour two."""
    supervisor = assistant.Supervisor(config, backoff=5.0, backoff_cap=40.0)
    seen = []
    for failures in range(1, 8):
        supervisor.failures = failures
        seen.append(supervisor.backoff_seconds())

    assert seen == [5.0, 10.0, 20.0, 40.0, 40.0, 40.0, 40.0]


def test_an_assistant_that_stayed_up_refills_the_budget(config, long_lived):
    """A daemon that runs for weeks must not accumulate its way to a give-up.

    An incarnation that lived past ``settled`` did its job, so its death is a
    death rather than a crash loop — and its successor starts at once, with no
    backoff to sit through.
    """
    clock = FakeClock()
    delays = []
    supervisor = assistant.Supervisor(
        config, clock=clock, sleep=delays.append, settled=60.0, poll=2.0,
    )
    supervisor.failures = 2  # two earlier attempts had already failed

    first = supervisor.first_start()
    try:
        clock.advance(3600)  # it ran for an hour
        kill(assistant.status().pid)
        assert spawn.wait_for_exit_recorded(first.session_id, 10.0), (
            "the exit was never recorded"
        )

        assert supervisor.supervise_once() is True
        second = assistant.status()
        assert supervisor.failures == 0
        assert delays == [], "a settled incarnation is replaced without a wait"
        assert second.session_id != first.session_id
        assert second.generation == first.generation + 1
    finally:
        kill_live_assistant()


def test_a_dead_assistant_is_respawned_without_being_asked(config, long_lived):
    """§8.1's "respawned if it dies", through the exit event rather than a poll."""
    supervisor = assistant.Supervisor(config, sleep=lambda _delay: None, poll=2.0)
    first = supervisor.first_start()
    kill(assistant.status().pid)
    assert spawn.wait_for_exit_recorded(first.session_id, 10.0), (
        "the exit was never recorded"
    )

    assert supervisor.supervise_once() is True
    second = assistant.status()
    try:
        assert second.running is True
        assert second.session_id != first.session_id
        assert second.generation == first.generation + 1
    finally:
        kill(second.pid)


def test_supervision_never_stops_the_assistant_it_is_watching(config, long_lived):
    """It only ever starts one, and that is not an omission.

    ``lifecycle.exit_session`` refuses ``kind="assistant"`` because ending one
    skips the pointer and ``stop_reason`` bookkeeping ``assistant.stop`` owns, and
    rotation on age or context pressure is §8.3's policy, not a supervision
    detail. A supervisor that terminated anything would route around both.
    """
    supervisor = assistant.Supervisor(
        config, await_exit=lambda _id, _timeout: False, sleep=lambda _delay: None,
        poll=0.01,
    )
    first = supervisor.first_start()
    try:
        for _ in range(3):
            assert supervisor.supervise_once() is True

        current = assistant.status()
        assert current.running is True
        assert current.session_id == first.session_id
        assert current.generation == first.generation, "no rotation happened here"
        assert events_of("assistant_stopped") == []
        assert assistant.read_state().stop_reason is None
    finally:
        kill_live_assistant()


def test_an_assistant_that_is_already_up_is_adopted_not_replaced(
    platform_root, fake_lmer
):
    """A survivor of the last daemon (spec R11) keeps its context window."""
    config = cfg.load({"lmer_bin": str(fake_lmer)})
    registry.register("s-survivor", kind=assistant.KIND, pid=os.getpid(),
                      started_at="2026-07-27T09:00:00Z")

    report = assistant.Supervisor(config).first_start()

    assert report.running is True
    assert report.adopted is True
    assert report.session_id == "s-survivor"
    assert "already running as s-survivor" in report.notice
    assert [e["id"] for e in registry.list_sessions()] == ["s-survivor"], (
        "one assistant at a time (D11) — nothing may spawn beside it"
    )


# --- (3) which cap the assistant spends --------------------------------------

def test_the_auto_started_assistant_does_not_spend_a_worker_slot(
    platform_root, fake_lmer, long_lived
):
    """``max_concurrent_sessions`` counts workers; the assistant is beside it (T75).

    The inversion of what this test proved at T63, and the reason it had to
    invert: the daemon starts the assistant at boot, so the slot it used to spend
    was spent for the life of the daemon — a host configured for one session ran
    *no* workers and a chat window. The cap is about how much work a host can
    bear, and the thing that routes the work is not work.

    The single slot is asserted from both ends. It is still free with the
    assistant live (the spawn below succeeds where it used to raise), and the
    startup notice says so rather than leaving an operator to work out which
    arithmetic this build uses.
    """
    config = cfg.load({
        "lmer_bin": str(fake_lmer),
        "max_concurrent_sessions": 1,
    })

    report = assistant.Supervisor(config).first_start()
    worker = None
    try:
        assert report.running is True
        assert "all 1 worker slot(s) remain" in report.notice
        worker = spawn.spawn_session(
            config,
            spawn.SpawnRequest(taskdef="develop", target="https://example.com/x"),
        )
        assert worker.session_id, (
            "the assistant was holding the host's only slot against a worker"
        )
    finally:
        kill(worker.pid if worker else None)
        kill_live_assistant()


def test_a_worker_that_fills_the_cap_still_refuses_another_worker(
    platform_root, fake_lmer, long_lived
):
    """The assistant's exemption is not the cap's: workers are counted as before.

    Paired with the test above so "the assistant does not count" cannot pass by
    the count having quietly stopped counting anything.
    """
    config = cfg.load({"lmer_bin": str(fake_lmer), "max_concurrent_sessions": 1})
    report = assistant.Supervisor(config).first_start()
    worker = None
    try:
        assert report.running is True
        worker = spawn.spawn_session(
            config,
            spawn.SpawnRequest(taskdef="develop", target="https://example.com/x"),
        )
        with pytest.raises(spawn.CapacityError, match="1/1"):
            spawn.spawn_session(
                config,
                spawn.SpawnRequest(taskdef="develop", target="https://example.com/y"),
            )
    finally:
        kill(worker.pid if worker else None)
        kill_live_assistant()


def test_a_live_assistant_does_not_refuse_its_own_replacement(
    platform_root, fake_lmer, long_lived
):
    """The exclusion runs in both directions, because it is one rule.

    An assistant entry that is still live — a previous incarnation, or a second
    start racing the first — must not be what refuses an assistant spawn either.
    A *second* assistant is refused, and by ``start``'s registry check (D11)
    rather than by a cap on workers: one setting meaning two things is how "the
    assistant may run two workers" turns into "the host may run two assistants".
    """
    config = cfg.load({"lmer_bin": str(fake_lmer), "max_concurrent_sessions": 1})
    report = assistant.Supervisor(config).first_start()
    try:
        assert report.running is True

        with pytest.raises(assistant.AssistantAlreadyRunning) as caught:
            assistant.start(config)
        assert "there is one at a time" in str(caught.value), (
            "the refusal must be the registry's, not the worker cap's"
        )
        assert events_of("assistant_start_refused")[-1]["data"]["reason"] == (
            "already_running"
        )

        # And the capacity check itself has nothing to say about it: the live
        # assistant is not in the count that would have refused the spawn.
        assert spawn._live_worker_count() == 0
    finally:
        kill_live_assistant()


def test_a_capacity_refusal_names_the_cap_that_refused_it(
    platform_root, fake_lmer, monkeypatch, capsys
):
    """The reservation is not an exemption: workers already holding every slot
    are holding the one the assistant wanted too, and "I cannot open the chat"
    needs the setting to raise in it.

    The retired cap is asserted absent from the output as well (T75): a build that
    reintroduced ``max_concurrent_assistant_spawns`` as an unenforced number would
    put it back in front of an operator as a thing to lower.
    """
    save_config(lmer_bin=str(fake_lmer), max_concurrent_sessions=1)
    registry.register("s-worker", kind="worker", pid=os.getpid())
    serve_nothing(monkeypatch)

    daemon.main(["run"])
    out = capsys.readouterr().out

    assert "max_concurrent_sessions (1)" in out
    assert "Free a worker slot" in out
    assert "assistant_spawns" not in out


# --- (4) the live assistant's environment is not rewritten under it ----------

def test_supervision_does_not_rewrite_a_live_assistants_environment(
    config, long_lived, monkeypatch
):
    """The auto-start runs on every boot, so this path is now the common one.

    ``_prepare_environment`` writes a 0600 file the live session read at launch,
    and the reachability answer can change under it — an operator rebinding the
    platform, or installing podman. Rewriting it would leave the file disagreeing
    with the session it describes, for no gain: nothing re-reads it.
    """
    pin_runtime(monkeypatch, "podman")
    cfg.ensure_secret(config)
    supervisor = assistant.Supervisor(
        config, await_exit=lambda _id, _timeout: False, sleep=lambda _delay: None,
        poll=0.01,
    )
    first = supervisor.first_start()
    try:
        env_file = assistant.env_file_path()
        before, stamp = env_file.read_bytes(), env_file.stat().st_mtime_ns
        assert "CANNOT reach" not in first.notice, "podman has a host alias"

        pin_runtime(monkeypatch, "docker")  # the derivation now answers differently
        again = supervisor.first_start()
        assert supervisor.supervise_once() is True

        assert again.adopted is True
        assert again.session_id == first.session_id
        assert env_file.read_bytes() == before
        assert env_file.stat().st_mtime_ns == stamp, (
            "the live assistant's environment file was rewritten under it"
        )
        values = dict(dotenv_values(str(env_file)))
        assert values[assistant.ENV_PLATFORM_URL].startswith(
            f"http://{cfg.PODMAN_HOST_ALIAS}"
        )
    finally:
        kill_live_assistant()


# --- (5) an unreachable platform still gets an assistant ---------------------

def test_an_unreachable_platform_still_gets_an_assistant(
    config, long_lived, monkeypatch
):
    """On docker there is no name for the host, and the session is told so (T30).

    It can still be chatted with and can still answer questions asked through the
    ask channel; what it cannot do is drive the API. Refusing to start it would
    trade a limitation for an absence.
    """
    pin_runtime(monkeypatch, "docker")
    cfg.ensure_secret(config)

    report = assistant.Supervisor(config).first_start()
    try:
        assert report.running is True
        assert report.reachable is False
        assert "CANNOT reach the platform API" in report.notice
        assert "--add-host" in report.notice, "name the cause, not just the fault"
        values = dict(dotenv_values(str(assistant.env_file_path())))
        assert assistant.ENV_PLATFORM_UNREACHABLE in values
        assert assistant.ENV_PLATFORM_URL not in values
    finally:
        kill_live_assistant()


def test_the_daemon_says_when_the_assistant_cannot_drive_the_platform(
    platform_root, fake_lmer, long_lived, monkeypatch, capsys
):
    """A chat window that cannot spawn anything is invisible otherwise.

    So it goes in the startup output beside the bind notice, where an operator
    reads it — not only into the session's own environment, where the only reader
    is an agent.
    """
    pin_runtime(monkeypatch, "docker")
    save_config(lmer_bin=str(fake_lmer))
    serve_nothing(monkeypatch)

    try:
        assert daemon.main(["run"]) == 0
        out = capsys.readouterr().out
        assert "CANNOT reach the platform API" in out
        assert "Assistant started as" in out, "it started anyway — that is the point"
    finally:
        kill_live_assistant()


# --- (6) a give-up is not the end of supervision (T75) -----------------------
#
# The hole T63 left. ``_give_up`` ends the watch thread and its message tells the
# operator to ``POST /api/assistant/start`` — which started a container that
# *nothing was watching*. That is worse than the give-up it appears to fix: the
# operator has been told once that the assistant needs attention, now believes they
# have fixed it, and the next crash is silent.

def test_a_manual_start_after_a_give_up_re_arms_supervision(
    platform_root, fake_lmer, monkeypatch
):
    """Through the route, because the route is the seam's only caller.

    The supervisor's own respawn goes through ``assistant.start`` too, so a re-arm
    living there would have each attempt refill the budget it was spending. "A
    human asked for this" is what licenses a fresh budget, and this is where that
    is known.

    Both halves are asserted: the counter is back to zero, and the watch is
    genuinely running again — the manually started incarnation is killed and
    something replaces it with nobody asking.
    """
    config = cfg.load({"lmer_bin": str(fake_lmer)})
    supervisor = assistant.Supervisor(
        config, clock=FakeClock(), sleep=lambda _delay: None, attempts=2, poll=0.5,
    )
    supervisor.run()
    assert supervisor.gave_up is True, "the stub exits at once — this must give up"
    assert supervisor.failures == 2
    monkeypatch.setattr(assistant, "_SUPERVISOR", supervisor)

    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")  # the manual one stays up
    try:
        response = client_for(config).post(
            "/api/assistant/start", headers=bearer_header()
        )
        assert response.status_code == 200, response.text
        manual = response.json()

        assert supervisor.gave_up is False, "the give-up has to be cleared"
        assert supervisor.failures == 0, (
            "a resumed loop one attempt from giving up again would spend its whole "
            "allowance on the first crash"
        )
        assert events_of("assistant_supervision_resumed"), (
            "the re-arm belongs in history beside the give-up it answers"
        )

        kill(manual["pid"])
        assert spawn.wait_for_exit_recorded(manual["session_id"], 10.0), (
            "the exit was never recorded"
        )
        respawned = wait_for_replacement(manual["session_id"])
        assert respawned.running is True, (
            "nothing respawned it: the manual start did not re-arm supervision"
        )
        assert respawned.generation > manual["generation"]
    finally:
        supervisor.stop()
        kill_live_assistant()


def test_a_start_on_a_daemon_that_supervises_nothing_just_works(
    platform_root, fake_lmer, long_lived
):
    """``lmer platform spawn``, an embedding caller and every route test.

    The seam must not become a new way for a start to fail, so "nothing is
    supervising" is an answer rather than an error.
    """
    config = cfg.load({"lmer_bin": str(fake_lmer)})
    assert assistant.resume_supervision() is False

    response = client_for(config).post(
        "/api/assistant/start", headers=bearer_header()
    )
    try:
        assert response.status_code == 200, response.text
        assert assistant.status().running is True
    finally:
        kill_live_assistant()


def test_supervision_that_is_still_watching_is_not_re_armed(
    config, long_lived, monkeypatch
):
    """Two loops would race each other's respawns and halve the backoff.

    So a re-arm on a healthy daemon is a no-op, and the operator's start is
    otherwise unaffected: the running loop sees the new incarnation on its own.
    """
    supervisor = assistant.Supervisor(
        config, await_exit=lambda _id, _timeout: False, sleep=lambda _delay: None,
        poll=0.01,
    )
    thread = supervisor.start()
    monkeypatch.setattr(assistant, "_SUPERVISOR", supervisor)
    try:
        assert assistant.resume_supervision() is False
        assert supervisor._thread is thread, "a second watch thread was started"
        assert events_of("assistant_supervision_resumed") == []
    finally:
        supervisor.stop()
        kill_live_assistant()


def test_a_stopped_supervisor_is_never_re_armed(config):
    """The daemon has finished serving, so a respawn would have nothing to serve it.

    Same reasoning as ``Supervisor.stop`` existing at all — and the daemon
    withdraws the supervisor there too, so this is the belt to that braces.
    """
    supervisor = assistant.Supervisor(config)
    supervisor.stop()

    assert supervisor.resume() is False
    assert supervisor.gave_up is False
    assert registry.list_sessions(live_only=True) == [], "nothing may have started"


def test_the_daemon_publishes_its_supervisor_and_withdraws_it(
    platform_root, fake_lmer, long_lived, monkeypatch
):
    """The route cannot re-arm what it cannot reach.

    Registered while serving, gone afterwards: a stopped supervisor left reachable
    would answer for a daemon on its way out, and in a process that runs this more
    than once it would answer for the previous run.
    """
    save_config(lmer_bin=str(fake_lmer))
    while_serving = {}
    serve_nothing(monkeypatch, lambda _kwargs: while_serving.update(
        supervisor=assistant._SUPERVISOR
    ))

    try:
        assert daemon.main(["run"]) == 0
        assert isinstance(while_serving["supervisor"], assistant.Supervisor)
        assert assistant._SUPERVISOR is None, (
            "the supervisor outlived the daemon that owned it"
        )
    finally:
        kill_live_assistant()
