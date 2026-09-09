"""Chat-thread connections the platform owns (issue #412, T1).

The records in ``threads.json`` and the connector processes reconciled against
them. The processes are fakes — what is pinned here is the daemon's side of the
contract: which argv and which environment a connector is given, when one is
started and stopped, what a refusal says, and that a fresh component over the
same file reattaches.
"""
from __future__ import annotations

import os
import stat

import pytest

from lmer_platform import connectors, registry, spawn, store
from slack_chat import registry as thread_registry
from tests.conftest import strip_lmer_env

SLACK_LINK = "https://example.slack.com/archives/C0123ABCDEF/p1712345678000900"
SLACK_LINK_QUERY = (
    "https://example.slack.com/archives/C0123ABCDEF/p1712345678000900"
    "?thread_ts=1712345678.000900"
)
MATRIX_LINK = "https://matrix.to/#/%21room%3Aexample.org/%24event"
#: A second Slack thread in the same channel, for the cases that need two.
OTHER_SLACK_LINK = "https://example.slack.com/archives/C0123ABCDEF/p1712345678000901"
CONTROL_TOKEN = "control-plane-token"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    monkeypatch.setattr(thread_registry, "REGISTRY_DIR", str(tmp_path / "threads"))
    return root


class FakeProcess:
    """Enough of ``Popen`` for the reconcile loop, and nothing else."""

    def __init__(self, argv, env):
        self.argv = list(argv)
        self.env = dict(env)
        # The daemon's own pid, so the thread registry reads the entry as live —
        # the fake process has no process behind it to keep alive.
        self.pid = os.getpid()
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise TimeoutError("still running")
        return self.returncode

    def exit(self, code=1):
        self.returncode = code


class Runner:
    def __init__(self):
        self.started = []

    def __call__(self, argv, env, log_path=None):
        process = FakeProcess(argv, env)
        self.started.append(process)
        return process


class FakeSlack:
    """Slack's answer for a thread: ``conversations.replies`` takes any message in
    it, and every threaded message carries the parent's ``thread_ts``."""

    def __init__(self, root=None):
        self.root = root
        self.asked = []

    def get_replies(self, channel, ts, oldest=None):
        self.asked.append((channel, ts))
        return [{"ts": ts, **({"thread_ts": self.root} if self.root else {})}]


class FakeMatrix:
    def __init__(self, *, encrypted=False, resolved="!room:example.org"):
        self.encrypted = encrypted
        self.resolved = resolved

    def resolve_room(self, room):
        return self.resolved

    def room_is_encrypted(self, room_id):
        return self.encrypted


def matrix_env():
    return {
        "LMER_MATRIX_HOMESERVER": "https://matrix.example.org",
        "LMER_MATRIX_USER_ID": "@lmer:example.org",
        "LMER_MATRIX_ACCESS_TOKEN": "secret-access-token",
    }


def live_session(session_id="s-1", *, port=8123, control=True, alive=True, run=None):
    payload = {
        "kind": "worker",
        "pid": os.getpid() if alive else 2 ** 22 - 1,
        "run": run or {"host": "gitlab.example.com", "project": "agents/global", "slug": "develop-412"},
        "task": {"taskdef": "develop", "target": "issue-412"},
        "started_at": "2026-09-05T09:00:00Z",
    }
    if control:
        payload["control"] = {
            "host": "127.0.0.1",
            "port": port,
            "token_ref": str(spawn.token_file_for(session_id)),
        }
    registry.register(session_id, **payload)
    if control:
        spawn.token_file_for(session_id).write_text(CONTROL_TOKEN, encoding="utf-8")
    return session_id


def record_signals(monkeypatch):
    """Capture signals sent to a pid, leaving the ``kill(pid, 0)`` liveness probe
    the registry itself uses to reach the real one."""
    sent = []
    real = connectors.os.kill

    def kill(pid, sig):
        if sig == 0:
            return real(pid, sig)
        sent.append((pid, sig))

    monkeypatch.setattr(connectors.os, "kill", kill)
    return sent


def component(platform_root, *, runner=None, environ=None, matrix=None, slack=None, clock=None):
    return connectors.ThreadConnectors(
        runner=runner or Runner(),
        environ=environ if environ is not None else {"SLACK_BOT_TOKEN": "xoxb-test"},
        matrix_client=lambda account: matrix or FakeMatrix(),
        slack_client=lambda token: slack or FakeSlack(),
        clock=clock or (lambda: 0.0),
    )


# ---------------------------------------------------------------------------
# Parsing and identity
# ---------------------------------------------------------------------------


class TestThreadRef:
    def test_the_same_thread_has_one_id_however_the_link_spells_it(self):
        assert (
            connectors.parse_thread(SLACK_LINK).id
            == connectors.parse_thread(SLACK_LINK_QUERY).id
        )

    def test_transports_never_share_an_id(self):
        slack = connectors.parse_thread(SLACK_LINK)
        matrix = connectors.parse_thread(MATRIX_LINK)
        assert slack.id.startswith("slack:") and matrix.id.startswith("matrix:")
        assert slack.id != matrix.id

    def test_a_link_that_is_not_a_thread_is_refused_by_shape(self):
        with pytest.raises(connectors.ThreadError) as caught:
            connectors.parse_thread("https://example.org/not-a-thread")
        assert caught.value.status == 400

    def test_embedded_credentials_are_refused(self):
        with pytest.raises(connectors.ThreadError):
            connectors.parse_thread(
                "https://user:pw@example.slack.com/archives/C0123ABCDEF/p1712345678000900"
            )

    def test_a_room_alias_resolves_to_the_room_id_so_one_room_is_one_connection(self):
        alias = connectors.parse_thread(
            "https://matrix.to/#/%23lmer%3Aexample.org/%24event",
            resolve_room=lambda room: "!room:example.org",
        )
        assert alias.id == connectors.parse_thread(MATRIX_LINK).id


# ---------------------------------------------------------------------------
# Connect
# ---------------------------------------------------------------------------


class TestConnect:
    def test_a_connect_records_starts_and_claims_the_thread(self, platform_root):
        live_session()
        runner = Runner()
        threads = component(platform_root, runner=runner)

        record = threads.connect("s-1", SLACK_LINK, by="operator")

        assert record["channel"] == "slack" and record["origin"] == "live"
        assert record["by"] == "operator" and record["running"] is True
        assert threads.records("s-1")[0]["permalink"] == SLACK_LINK
        started = runner.started[0]
        assert started.argv[1:3] == ["-m", "slack_chat.connector"]
        assert started.argv[3:5] == ["--permalink", SLACK_LINK]
        assert started.env["LMER_SUPERVISOR_URL"] == "http://127.0.0.1:8123"
        assert started.env["LMER_FASTAPI_TOKEN"] == CONTROL_TOKEN
        assert started.env["SLACK_BOT_TOKEN"] == "xoxb-test"
        assert thread_registry.is_thread_connected("C0123ABCDEF", "1712345678.000900")

    def test_the_control_token_never_reaches_the_command_line(self, platform_root):
        live_session()
        runner = Runner()
        component(platform_root, runner=runner).connect("s-1", SLACK_LINK, by="operator")
        assert CONTROL_TOKEN not in " ".join(runner.started[0].argv)

    def test_the_record_file_is_owner_only(self, platform_root):
        live_session()
        component(platform_root).connect("s-1", SLACK_LINK, by="operator")
        mode = stat.S_IMODE(connectors.threads_path().stat().st_mode)
        assert mode == 0o600

    def test_the_same_thread_twice_on_one_run_is_refused(self, platform_root):
        live_session()
        threads = component(platform_root)
        threads.connect("s-1", SLACK_LINK, by="operator")
        with pytest.raises(connectors.ThreadError) as caught:
            threads.connect("s-1", SLACK_LINK_QUERY, by="operator")
        assert caught.value.status == 409
        assert "already connected" in str(caught.value)

    def test_a_thread_another_live_run_holds_is_refused_by_name(self, platform_root):
        live_session("s-1")
        live_session("s-2", run={"host": "gitlab.example.com", "project": "agents/global", "slug": "review-99"})
        threads = component(platform_root)
        threads.connect("s-1", SLACK_LINK, by="operator")
        with pytest.raises(connectors.ThreadError) as caught:
            threads.connect("s-2", SLACK_LINK, by="operator")
        assert caught.value.status == 409
        assert "develop-412" in str(caught.value)

    def test_a_thread_whose_holder_died_is_free_again(self, platform_root):
        live_session("s-1")
        live_session("s-2")
        threads = component(platform_root)
        threads.connect("s-1", SLACK_LINK, by="operator")
        registry.register("s-1", pid=2 ** 22 - 1, kind="worker", started_at="2026-09-05T09:00:00Z")
        thread_registry.deregister("C0123ABCDEF", "1712345678.000900", pid=os.getpid())
        assert threads.connect("s-2", SLACK_LINK, by="operator")["running"] is True

    def test_a_missing_slack_token_names_the_variable(self, platform_root):
        live_session()
        threads = component(platform_root, environ={})
        with pytest.raises(connectors.ThreadError) as caught:
            threads.connect("s-1", SLACK_LINK, by="operator")
        assert "SLACK_BOT_TOKEN" in str(caught.value)

    def test_missing_matrix_credentials_name_all_three(self, platform_root):
        live_session()
        threads = component(platform_root, environ={})
        with pytest.raises(connectors.ThreadError) as caught:
            threads.connect("s-1", MATRIX_LINK, by="operator")
        message = str(caught.value)
        assert "LMER_MATRIX_HOMESERVER" in message
        assert "LMER_MATRIX_ACCESS_TOKEN" in message

    def test_an_encrypted_room_is_refused_by_name(self, platform_root):
        live_session()
        threads = component(
            platform_root, environ=matrix_env(), matrix=FakeMatrix(encrypted=True),
        )
        with pytest.raises(connectors.ThreadError) as caught:
            threads.connect("s-1", MATRIX_LINK, by="operator")
        assert "!room:example.org" in str(caught.value)
        assert "encrypted" in str(caught.value)

    def test_a_session_with_no_control_plane_is_refused(self, platform_root):
        live_session("s-1", control=False)
        with pytest.raises(connectors.ThreadError) as caught:
            component(platform_root).connect("s-1", SLACK_LINK, by="operator")
        assert caught.value.status == 409

    def test_a_refused_connect_leaves_no_record(self, platform_root):
        live_session()
        threads = component(platform_root, environ={})
        with pytest.raises(connectors.ThreadError):
            threads.connect("s-1", SLACK_LINK, by="operator")
        assert threads.records("s-1") == []

    def test_the_per_session_bound_is_the_launch_list_bound(self, platform_root):
        live_session()
        threads = component(platform_root)
        for index in range(connectors.MAX_PER_SESSION):
            threads.connect(
                "s-1",
                f"https://example.slack.com/archives/C0123ABCDEF/p17123456780{index:05d}",
                by="operator",
            )
        with pytest.raises(connectors.ThreadError) as caught:
            threads.connect("s-1", SLACK_LINK, by="operator")
        assert str(connectors.MAX_PER_SESSION) in str(caught.value)


# ---------------------------------------------------------------------------
# Disconnect and reconcile
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_disconnect_drops_the_record_and_asks_the_process_to_go(self, platform_root):
        live_session()
        runner = Runner()
        threads = component(platform_root, runner=runner)
        record = threads.connect("s-1", SLACK_LINK, by="operator")

        gone = threads.disconnect("s-1", record["id"])

        assert gone["id"] == record["id"]
        assert threads.records("s-1") == []
        # SIGTERM, never a kill: the seconds are what the connector needs to post
        # its "disconnected" line in the thread.
        assert runner.started[0].terminated and not runner.started[0].killed
        assert not thread_registry.is_thread_connected("C0123ABCDEF", "1712345678.000900")

    def test_an_unknown_connection_is_a_404(self, platform_root):
        live_session()
        with pytest.raises(connectors.ThreadError) as caught:
            component(platform_root).disconnect("s-1", "slack:deadbeefdeadbeef")
        assert caught.value.status == 404

    def test_a_session_that_stops_being_live_keeps_its_record_and_loses_its_connector(
        self, platform_root
    ):
        live_session()
        runner = Runner()
        threads = component(platform_root, runner=runner)
        threads.connect("s-1", SLACK_LINK, by="operator")

        registry.register("s-1", pid=2 ** 22 - 1, kind="worker", started_at="2026-09-05T09:00:00Z")
        threads.reconcile()

        assert runner.started[0].terminated
        assert [record["id"] for record in threads.records("s-1")]
        assert threads.listing("s-1")[0]["running"] is False

    def test_a_connector_that_exits_is_restarted_after_its_backoff(self, platform_root):
        live_session()
        runner = Runner()
        now = {"t": 0.0}
        threads = component(platform_root, runner=runner, clock=lambda: now["t"])
        threads.connect("s-1", SLACK_LINK, by="operator")

        runner.started[0].exit(2)
        threads.reconcile()
        assert len(runner.started) == 1, "a just-exited connector waits out its backoff"
        assert threads.listing("s-1")[0]["last_exit"] == 2

        now["t"] = 60.0
        threads.reconcile()
        assert len(runner.started) == 2

    def test_a_process_with_no_record_is_stopped(self, platform_root):
        live_session()
        runner = Runner()
        threads = component(platform_root, runner=runner)
        record = threads.connect("s-1", SLACK_LINK, by="operator")
        threads._sessions["s-1"] = []

        threads.reconcile()

        assert runner.started[0].terminated
        assert record["id"] not in {key[1] for key in threads._procs}

    def test_a_fresh_component_over_the_same_file_reattaches(self, platform_root):
        live_session()
        component(platform_root).connect("s-1", SLACK_LINK, by="operator")

        runner = Runner()
        again = component(platform_root, runner=runner)
        assert [record["id"] for record in again.records("s-1")]
        again.reconcile()
        assert runner.started[0].argv[3:5] == ["--permalink", SLACK_LINK]

    def test_records_for_a_session_this_host_forgot_are_pruned(self, platform_root):
        live_session("s-1")
        live_session("s-2")
        threads = component(platform_root)
        threads.connect("s-1", SLACK_LINK, by="operator")
        registry.remove("s-1", force=True)

        threads.reconcile()

        assert threads.records("s-1") == []

    def test_a_registry_that_lists_nothing_prunes_nothing(self, platform_root):
        """An empty listing is a read that failed far more often than it is a
        host that has forgotten every session, and pruning on it would drop the
        connections of every live run at once."""
        live_session()
        threads = component(platform_root)
        threads.connect("s-1", SLACK_LINK, by="operator")
        registry.remove("s-1", force=True)

        threads.reconcile()

        assert [record["permalink"] for record in threads.records("s-1")] == [SLACK_LINK]


# ---------------------------------------------------------------------------
# Launch targets
# ---------------------------------------------------------------------------


class TestLaunchTargets:
    def test_launch_targets_are_recorded_as_connections(self, platform_root):
        live_session()
        threads = component(platform_root)
        recorded = threads.adopt_launch_targets("s-1", [SLACK_LINK], by="operator")
        assert [record["origin"] for record in recorded] == ["launch"]
        assert threads.records("s-1")[0]["permalink"] == SLACK_LINK

    def test_a_repeated_link_connects_once(self, platform_root):
        live_session()
        threads = component(platform_root)
        threads.adopt_launch_targets("s-1", [SLACK_LINK, SLACK_LINK_QUERY], by="operator")
        assert len(threads.records("s-1")) == 1

    def test_a_launch_target_naming_a_room_alias_keys_on_the_room_id(self, platform_root):
        """Otherwise the same room reached by alias at launch and by id from the
        UI would be two connections — two connectors, and every reply twice."""
        live_session()
        threads = component(platform_root, environ=matrix_env())
        threads.adopt_launch_targets(
            "s-1", ["https://matrix.to/#/%23lmer%3Aexample.org/%24event"], by="operator",
        )
        assert threads.records("s-1")[0]["id"] == connectors.parse_thread(MATRIX_LINK).id

    def test_an_alias_the_homeserver_cannot_resolve_still_records(self, platform_root):
        """A spawn must not fail over a directory lookup."""
        class Unreachable(FakeMatrix):
            def resolve_room(self, room):
                raise RuntimeError("homeserver is down")

        live_session()
        threads = component(platform_root, environ=matrix_env(), matrix=Unreachable())
        recorded = threads.adopt_launch_targets(
            "s-1", ["https://matrix.to/#/%23lmer%3Aexample.org/%24event"], by="operator",
        )
        assert [record["key"]["room"] for record in recorded] == ["#lmer:example.org"]

    def test_a_channel_slack_refuses_is_still_recorded_at_launch(self, platform_root):
        """The scoping note on !299's round-2 major: a live *connect* refuses on
        Slack's `not_in_channel`, and a launch target must not. A spawn's target
        is recorded rather than decided — the session is already starting, and
        the runtime block is where the operator learns the connector will not
        run."""
        class Refuses(FakeSlack):
            def get_replies(self, channel, ts, oldest=None):
                from slack_chat.client import SlackError

                raise SlackError("Slack API error: not_in_channel", code="not_in_channel")

        live_session()
        threads = component(platform_root, slack=Refuses())

        recorded = threads.adopt_launch_targets("s-1", [SLACK_LINK], by="operator")

        assert [record["permalink"] for record in recorded] == [SLACK_LINK]

    def test_an_unusable_target_is_dropped_rather_than_failing_the_spawn(self, platform_root):
        live_session()
        threads = component(platform_root)
        assert threads.adopt_launch_targets("s-1", ["not-a-link"], by="operator") == []
        assert threads.records("s-1") == []


class TestDaemonWiring:
    def test_the_daemon_publishes_the_component_and_reconciles_on_start(
        self, platform_root, monkeypatch,
    ):
        """A daemon restart with connections recorded reattaches them, and that
        is not a special path — it is the first pass of the ordinary loop.

        The loop itself is stubbed: this component is the daemon's, so its runner
        and its Slack client are the real ones, and letting a tick land would
        spawn an actual connector process against an actual workspace.
        """
        from lmer_platform import daemon

        monkeypatch.setattr(connectors.ThreadConnectors, "reconcile", lambda self: None)
        live_session()
        component(platform_root).connect("s-1", SLACK_LINK, by="operator")

        threads = daemon._run_thread_connectors()
        try:
            assert connectors.get_connectors() is threads
            assert [record["permalink"] for record in threads.records("s-1")] == [SLACK_LINK]
        finally:
            threads.stop()
            connectors.register_connectors(None)


class TestForgettingARun:
    def test_a_forgotten_runs_records_go_with_it(self, platform_root):
        """A crashed session's entry is kept on purpose, so the prune would leave
        the records of a run this orchestrator has been told to forget."""
        from lmer_platform import runs

        live_session()
        runs.track(
            "gitlab.example.com", "agents/global", "develop-412",
            source="spawned", session_id="s-1",
        )
        threads = component(platform_root)
        threads.connect("s-1", SLACK_LINK, by="operator")

        dropped = threads.drop_run(("gitlab.example.com", "agents/global", "develop-412"))

        assert dropped == 1 and threads.records("s-1") == []

    def test_another_runs_records_are_left_alone(self, platform_root):
        from lmer_platform import runs

        live_session()
        runs.track(
            "gitlab.example.com", "agents/global", "develop-412",
            source="spawned", session_id="s-1",
        )
        threads = component(platform_root)
        threads.connect("s-1", SLACK_LINK, by="operator")

        assert threads.drop_run(("gitlab.example.com", "agents/global", "other")) == 0
        assert threads.records("s-1")


class TestReclaimingAnOrphan:
    """Review finding: a connector is a detached process, so a daemon killed by
    signal leaves it running — and the restart used to start a *second* one for
    the same thread, doubling every post and every injected reply."""

    def test_a_platform_orphan_is_asked_to_stop_and_the_start_waits(self, platform_root, monkeypatch):
        live_session()
        runner = Runner()
        threads = component(platform_root, runner=runner)
        thread_registry.register(
            "C0123ABCDEF", "1712345678.000900", permalink=SLACK_LINK,
            pid=4321, owner=connectors.OWNER,
        )
        monkeypatch.setattr(connectors.ThreadConnectors, "_is_our_connector", lambda *a: True)
        signalled = record_signals(monkeypatch)
        monkeypatch.setattr(
            connectors.thread_registry, "_pid_alive", lambda pid: pid == 4321,
        )

        threads.adopt_launch_targets("s-1", [SLACK_LINK], by="operator")
        threads.reconcile()

        assert signalled == [(4321, connectors.signal.SIGTERM)]
        assert runner.started == [], "the replacement waits for the orphan to go"
        assert "asked to stop" in threads.listing("s-1")[0]["detail"]

    def test_a_recycled_pid_is_never_signalled(self, platform_root, monkeypatch):
        """The registry documents pid reuse as an accepted edge because its only
        consumer *reads*. Signalling on the same evidence is not safe, so the
        process is checked against what it actually is.

        The holder here is **this** process: alive, with a readable argv that is
        demonstrably not a connector. An absent pid would exercise the
        *unreadable* branch instead — which is a different answer with a
        different consequence, and has its own test below.
        """
        live_session()
        runner = Runner()
        threads = component(platform_root, runner=runner)
        thread_registry.register(
            "C0123ABCDEF", "1712345678.000900", permalink=SLACK_LINK,
            pid=os.getpid(), owner=connectors.OWNER,
        )
        signalled = record_signals(monkeypatch)

        # The branch under test, named rather than assumed: the argv was read,
        # and it is not ours.
        assert threads._is_our_connector(
            os.getpid(), connectors.parse_thread(SLACK_LINK),
        ) is False

        threads.adopt_launch_targets("s-1", [SLACK_LINK], by="operator")
        threads.reconcile()

        assert signalled == [], "a process that is not a connector is never signalled"
        assert len(runner.started) == 1, "a stale claim leaves the thread free"

    def test_a_cli_holder_is_left_alone(self, platform_root, monkeypatch):
        live_session()
        runner = Runner()
        threads = component(platform_root, runner=runner)
        thread_registry.register(
            "C0123ABCDEF", "1712345678.000900", permalink=SLACK_LINK, pid=4321,
        )
        monkeypatch.setattr(
            connectors.thread_registry, "_pid_alive", lambda pid: pid == 4321,
        )
        signalled = record_signals(monkeypatch)

        threads.adopt_launch_targets("s-1", [SLACK_LINK], by="operator")
        threads.reconcile()

        assert signalled == []
        assert runner.started == []
        assert "did not start" in threads.listing("s-1")[0]["detail"]

    def test_a_connector_that_exits_lets_go_of_its_thread(self, platform_root):
        """Its entry used to outlive it, leaving the next start to reason about a
        pid nobody owns."""
        live_session()
        runner = Runner()
        threads = component(platform_root, runner=runner)
        threads.connect("s-1", SLACK_LINK, by="operator")
        runner.started[0].exit(2)

        threads.reconcile()

        assert not thread_registry.is_thread_connected("C0123ABCDEF", "1712345678.000900")


class TestOneLiveSessionPerThread:
    """A thread has one live session at a time (issue #413).

    The refusal is at the door — a launch naming a held thread never starts —
    and behind it the reconcile will not take a thread off a session that is
    still using it, which is what the two registrations used to do to each other
    several times a minute.
    """

    def test_a_launch_target_a_live_run_holds_is_refused_by_name(self, platform_root):
        live_session("s-1")
        threads = component(platform_root)
        threads.connect("s-1", SLACK_LINK, by="operator")

        with pytest.raises(connectors.ThreadError) as caught:
            threads.refuse_held_targets([SLACK_LINK_QUERY])

        assert caught.value.status == 409
        assert "develop-412" in str(caught.value)

    def test_a_holder_outside_this_platform_is_refused(self, platform_root, monkeypatch):
        threads = component(platform_root)
        thread_registry.register(
            "C0123ABCDEF", "1712345678.000900", permalink=SLACK_LINK, pid=4321,
        )
        monkeypatch.setattr(
            connectors.thread_registry, "_pid_alive", lambda pid: pid == 4321,
        )

        with pytest.raises(connectors.ThreadError) as caught:
            threads.refuse_held_targets([SLACK_LINK])

        assert caught.value.status == 409
        assert "outside this platform" in str(caught.value)

    def test_a_connector_winding_down_for_a_dead_session_refuses_nothing(
        self, platform_root, monkeypatch
    ):
        """Every rotation passes through this state, so refusing on it would make
        the assistant un-startable rather than merely late: `rotate` stops the
        incumbent and starts its successor while the incumbent's connectors still
        have their stop grace. `_reclaim` is what deals with that one."""
        live_session("s-1")
        runner = Runner()
        threads = component(platform_root, runner=runner)
        threads.connect("s-1", SLACK_LINK, by="operator")
        registry.remove("s-1")
        monkeypatch.setattr(
            connectors.thread_registry, "_pid_alive", lambda pid: True,
        )

        threads.refuse_held_targets([SLACK_LINK])

    def test_an_unusable_target_is_not_this_checks_refusal(self, platform_root):
        threads = component(platform_root)

        threads.refuse_held_targets(["https://example.org/not-a-thread"])

    def test_a_second_sessions_record_never_takes_the_connector(
        self, platform_root, monkeypatch
    ):
        """The regression: two live sessions, one thread. The second attaches
        nothing, the thread is told nothing, and the first is left alone.

        ``_is_our_connector`` is forced true, as the orphan tests do it, so that
        "never signalled" is an assertion rather than a coincidence: the holder
        here is the pytest process, whose argv carries no permalink, so without
        the stub the pre-guard path reads it as a stale claim and never reaches
        ``os.kill`` either way.
        """
        live_session("s-1")
        live_session(
            "s-2",
            run={"host": "gitlab.example.com", "project": "agents/global", "slug": "review-99"},
        )
        runner = Runner()
        threads = component(platform_root, runner=runner)
        threads.connect("s-1", SLACK_LINK, by="operator")
        holder = runner.started[0]
        monkeypatch.setattr(connectors.ThreadConnectors, "_is_our_connector", lambda *a: True)
        signalled = record_signals(monkeypatch)

        threads.adopt_launch_targets("s-2", [SLACK_LINK], by="operator")
        threads.reconcile()

        assert signalled == [], "a live session's connector is never signalled"
        assert not holder.terminated and holder.poll() is None
        assert len(runner.started) == 1, "nothing new attaches to a held thread"
        assert threads.listing("s-1")[0]["running"] is True
        deferred = threads.listing("s-2")[0]
        assert deferred["running"] is False
        assert "develop-412" in deferred["detail"]

    def test_a_recycled_pid_on_another_thread_does_not_hold_this_one(
        self, platform_root,
    ):
        """The holder question is "this thread, this pid", not the pid alone.

        A stale claim on thread Y naming a pid the OS has since given to a
        connector for thread X would otherwise read as Y's holder — and this
        answer stands in front of ``_is_our_connector``, which is what catches a
        recycled pid. Y would then be refused for as long as X's connector lives,
        with a detail naming the wrong run.
        """
        live_session("s-1")
        live_session("s-2")
        runner = Runner()
        threads = component(platform_root, runner=runner)
        threads.connect("s-1", SLACK_LINK, by="operator")
        stale = connectors.parse_thread(OTHER_SLACK_LINK)
        thread_registry.register(
            *stale.registry_key(),
            permalink=OTHER_SLACK_LINK,
            pid=runner.started[0].pid,
            kind=stale.channel,
            owner=connectors.OWNER,
        )

        threads.adopt_launch_targets("s-2", [OTHER_SLACK_LINK], by="operator")
        threads.reconcile()

        assert len(runner.started) == 2, "the stale claim is not this thread's holder"
        assert threads.listing("s-2")[0]["running"] is True

    def test_the_deferred_connection_attaches_once_the_holder_goes(self, platform_root):
        """What makes the deferral a wait rather than a loss: the record stays,
        and the ordinary backoff brings it round when the thread is free."""
        live_session("s-1")
        live_session("s-2")
        now = {"t": 0.0}
        runner = Runner()
        threads = component(platform_root, runner=runner, clock=lambda: now["t"])
        threads.connect("s-1", SLACK_LINK, by="operator")
        threads.adopt_launch_targets("s-2", [SLACK_LINK], by="operator")
        threads.reconcile()
        assert len(runner.started) == 1

        registry.register(
            "s-1", pid=2 ** 22 - 1, kind="worker", started_at="2026-09-05T09:00:00Z",
        )
        now["t"] = connectors.BACKOFF_MAX + 1.0
        threads.reconcile()

        assert len(runner.started) == 2
        assert threads.listing("s-2")[0]["running"] is True


class TestRuntimeFacts:
    def test_a_restart_clears_the_previous_exit_code(self, platform_root):
        live_session()
        runner = Runner()
        now = {"t": 0.0}
        threads = component(platform_root, runner=runner, clock=lambda: now["t"])
        threads.connect("s-1", SLACK_LINK, by="operator")
        runner.started[0].exit(2)
        threads.reconcile()
        assert threads.listing("s-1")[0]["last_exit"] == 2

        now["t"] = 60.0
        threads.reconcile()

        view = threads.listing("s-1")[0]
        assert view["running"] is True and view["last_exit"] is None

    def test_a_dropped_connection_takes_its_runtime_facts_with_it(self, platform_root):
        live_session()
        runner = Runner()
        threads = component(platform_root, runner=runner)
        record = threads.connect("s-1", SLACK_LINK, by="operator")
        runner.started[0].exit(2)
        threads.reconcile()

        threads.disconnect("s-1", record["id"])

        assert threads._exits == {} and threads._detail == {}


class TestTheRecordShape:
    def test_both_creation_paths_write_the_same_keys(self, platform_root):
        """It is the persisted schema — what `_is_record` validates and what the
        routes serialise — so a field added to one path and not the other reads
        back as valid and is missing something."""
        live_session("s-1")
        live_session("s-2")
        threads = component(platform_root)
        live = threads.connect("s-1", SLACK_LINK, by="operator")
        launched = threads.adopt_launch_targets("s-2", [SLACK_LINK], by="operator")[0]
        assert set(live) - {"running", "since", "last_exit", "detail"} == set(launched)
        assert live["origin"] == "live" and launched["origin"] == "launch"


class TestTheThreadSlackReports:
    """Review finding: a permalink to a message *inside* a thread with no
    ``thread_ts`` on it parses as its own root, so one conversation could be
    connected twice under two ids — and every guard here is a lookup on that id.
    The same failure the matrix alias fix prevents, on the other transport."""

    def test_a_reply_permalink_keys_on_the_root_slack_names(self, platform_root):
        live_session()
        slack = FakeSlack(root="1712345678.000900")
        threads = component(platform_root, slack=slack)

        reply_link = "https://example.slack.com/archives/C0123ABCDEF/p1712345699000100"
        record = threads.connect("s-1", reply_link, by="operator")

        assert record["id"] == connectors.parse_thread(SLACK_LINK).id
        assert record["key"]["thread_ts"] == "1712345678.000900"
        # The link the operator pasted is kept: it is what opens the thread.
        assert record["permalink"] == reply_link
        assert slack.asked == [("C0123ABCDEF", "1712345699.000100")]

    def test_the_root_link_and_a_reply_link_are_one_connection(self, platform_root):
        live_session()
        threads = component(platform_root, slack=FakeSlack(root="1712345678.000900"))
        threads.connect("s-1", SLACK_LINK, by="operator")

        with pytest.raises(connectors.ThreadError) as caught:
            threads.connect(
                "s-1", "https://example.slack.com/archives/C0123ABCDEF/p1712345699000100",
                by="operator",
            )
        assert caught.value.status == 409

    def test_a_root_link_is_left_exactly_as_it_was(self, platform_root):
        """Slack reports the root's own ts for a root, so nothing moves."""
        live_session()
        threads = component(platform_root, slack=FakeSlack())
        record = threads.connect("s-1", SLACK_LINK, by="operator")
        assert record["id"] == connectors.parse_thread(SLACK_LINK).id

    def test_a_workspace_that_cannot_be_asked_still_connects(self, platform_root):
        """A directory lookup must not be what makes a connect fail; the link's
        own reading stands."""
        class Unreachable:
            def get_replies(self, channel, ts, oldest=None):
                raise RuntimeError("slack said no")

        live_session()
        threads = component(platform_root, slack=Unreachable())
        record = threads.connect("s-1", SLACK_LINK, by="operator")
        assert record["id"] == connectors.parse_thread(SLACK_LINK).id


class TestAnAttachmentRemembersWhereItGot:
    """Review iteration 2, measured: a connector's *clean* stop acks ``final``,
    and ``MessageLog.ack`` removes the subscriber name — so a graceful daemon
    restart erased the very fact the next start needed, and a launch-origin
    connection came back at the beginning and re-posted the session's whole
    journal into its thread. The record is where the position survives that."""

    def health(self, session_id, subscribers):
        return {"comms": {"schema": 1, "seq": 6, "subscribers": subscribers}}

    def test_the_position_is_taken_off_the_supervisor_and_kept(self, platform_root, monkeypatch):
        live_session()
        threads = component(platform_root)
        record = threads.adopt_launch_targets("s-1", [SLACK_LINK], by="operator")[0]
        monkeypatch.setattr(
            connectors.session_io, "_quiet_health",
            lambda sid: self.health(sid, {record["id"]: 3}),
        )

        threads.reconcile()

        assert threads.records("s-1")[0]["cursor"] == 3

    def test_a_graceful_restart_of_a_launch_connection_does_not_replay(
        self, platform_root, monkeypatch,
    ):
        """The whole finding, end to end: read three, stop cleanly (the ack that
        removes the name), restart, and the connector is told to carry on from
        three rather than from the beginning."""
        live_session()
        runner = Runner()
        threads = component(platform_root, runner=runner)
        record = threads.adopt_launch_targets("s-1", [SLACK_LINK], by="operator")[0]
        monkeypatch.setattr(
            connectors.session_io, "_quiet_health",
            lambda sid: self.health(sid, {record["id"]: 3}),
        )
        threads.reconcile()
        threads.stop()

        # The clean stop is what takes the supervisor's answer away.
        monkeypatch.setattr(
            connectors.session_io, "_quiet_health", lambda sid: self.health(sid, {}),
        )
        again = component(platform_root, runner=(restarted := Runner()))
        again.reconcile()

        env = restarted.started[0].env
        assert env["LMER_CHAT_ATTACH_SINCE"] == "3"
        assert env["LMER_CHAT_ATTACH_FROM_NOW"] == "0", "a launch connection, unchanged"

    def test_a_position_never_goes_backwards(self, platform_root, monkeypatch):
        live_session()
        threads = component(platform_root)
        record = threads.adopt_launch_targets("s-1", [SLACK_LINK], by="operator")[0]
        monkeypatch.setattr(
            connectors.session_io, "_quiet_health",
            lambda sid: self.health(sid, {record["id"]: 5}),
        )
        threads.reconcile()
        monkeypatch.setattr(
            connectors.session_io, "_quiet_health",
            lambda sid: self.health(sid, {record["id"]: 2}),
        )
        threads.reconcile()
        assert threads.records("s-1")[0]["cursor"] == 5

    def test_a_session_with_no_connections_is_never_asked(self, platform_root, monkeypatch):
        """One health read per session that has connections, and none for the
        rest — most sessions have none."""
        live_session()
        threads = component(platform_root)
        asked = []
        monkeypatch.setattr(
            connectors.session_io, "_quiet_health",
            lambda sid: asked.append(sid) or self.health(sid, {}),
        )
        threads.reconcile()
        assert asked == []


class TestTheConnectorIsGivenTheResolvedThread:
    def test_a_reply_link_reaches_the_connector_as_its_root(self, platform_root):
        """Otherwise the connector polls a *message*: `conversations.replies`
        returns only the one named when it has no replies of its own, so the
        thread's other replies never arrive."""
        live_session()
        runner = Runner()
        threads = component(
            platform_root, runner=runner, slack=FakeSlack(root="1712345678.000900"),
        )
        threads.connect(
            "s-1", "https://example.slack.com/archives/C0123ABCDEF/p1712345699000100",
            by="operator",
        )
        assert runner.started[0].argv[4] == SLACK_LINK

    def test_the_subscriber_name_is_the_connection_id(self, platform_root):
        """What makes one `/healthz` read answer for every connection on a
        session, by id, with nothing to map between."""
        import hashlib

        from slack_chat import connector as slack_connector
        from slack_chat.permalink import parse_slack_permalink

        ref = connectors.parse_thread(SLACK_LINK)
        channel, ts = parse_slack_permalink(ref.canonical)
        digest = hashlib.sha256(f"{channel}:{ts}".encode("utf-8")).hexdigest()[:16]
        assert f"{slack_connector.SUBSCRIBER}:{digest}" == ref.id

    def test_a_matrix_alias_reaches_the_connector_as_the_room_id(self, platform_root):
        live_session()
        runner = Runner()
        threads = component(platform_root, runner=runner, environ=matrix_env())
        threads.connect(
            "s-1", "https://matrix.to/#/%23lmer%3Aexample.org/%24event", by="operator",
        )
        assert "%21room%3Aexample.org" in runner.started[0].argv[4]


class TestAnUnreadableHolder:
    def test_it_is_not_read_as_somebody_elses_process(self, platform_root, monkeypatch):
        """Review iteration 2: "could not read the argv" was collapsed into "not
        ours", so the thread was treated as free and a second connector started —
        the duplicate the whole path exists to prevent."""
        live_session()
        runner = Runner()
        threads = component(platform_root, runner=runner)
        thread_registry.register(
            "C0123ABCDEF", "1712345678.000900", permalink=SLACK_LINK,
            pid=4321, owner=connectors.OWNER,
        )
        monkeypatch.setattr(
            connectors.thread_registry, "_pid_alive", lambda pid: pid == 4321,
        )
        monkeypatch.setattr(connectors.ThreadConnectors, "_is_our_connector", lambda *a: None)
        signalled = record_signals(monkeypatch)

        threads.adopt_launch_targets("s-1", [SLACK_LINK], by="operator")
        threads.reconcile()

        assert runner.started == [], "nothing is started over a holder nobody can identify"
        assert signalled == [], "and nothing is signalled either"
        assert "cannot read what it is" in threads.listing("s-1")[0]["detail"]

    def test_a_pid_that_vanished_leaves_the_thread_free(self, platform_root, monkeypatch):
        """A gone process is a known answer, not an unreadable one."""
        live_session()
        threads = component(platform_root)
        monkeypatch.setattr(
            connectors.Path, "read_bytes",
            lambda self: (_ for _ in ()).throw(FileNotFoundError()),
        )
        assert threads._is_our_connector(4321, connectors.parse_thread(SLACK_LINK)) is False
