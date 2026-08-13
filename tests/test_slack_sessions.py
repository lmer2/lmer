"""Tests for slack_chat.sessions (lmer chat session lifecycle)."""

import asyncio
import json
import os
import stat
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from lmer_cli.presets import Preset, load_presets, select_preset_name
from slack_chat.sessions import (
    CHAT_TASK_ID,
    Session,
    SessionManager,
    _env_int,
    listener_default_preset,
)


def _fake_session(
    channel: str = "C1",
    thread_ts: str = "111.222",
    returncode=None,
) -> Session:
    """Build a Session around a stub process without spawning anything."""

    class _StubProcess:
        def __init__(self, rc):
            self.returncode = rc
            self.pid = 99999

        async def wait(self):
            return self.returncode

    read_fd, write_fd = os.pipe()
    # Keep the write end open so reads would block; tests never read it.
    session = Session(
        channel=channel,
        thread_ts=thread_ts,
        permalink="https://x.slack.com/archives/C1/p1112220000000000",
        process=_StubProcess(returncode),
        master_fd=read_fd,
        log_path=Path("/tmp/unused.log"),
    )
    session._test_write_fd = write_fd  # keep a reference for cleanup
    return session


def _write_recording_lmer(tmp_path: Path, argv_log: Path) -> Path:
    """A stand-in lmer that records the argv it was called with, then sleeps."""
    script = tmp_path / "recording-lmer"
    script.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$@" > "{argv_log}"\n'
        "sleep 60\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


async def _read_recorded_argv(argv_log: Path, timeout: float = 3.0) -> list[str]:
    """Poll until the recording fake-lmer has written its argv, then return it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if argv_log.exists():
            text = argv_log.read_text()
            if text.strip():
                return text.splitlines()
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"fake lmer did not record argv at {argv_log} within {timeout}s"
    )


class TestEnvInt:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("SOME_INT_VAR", raising=False)
        assert _env_int("SOME_INT_VAR", 7) == 7

    def test_parses_value(self, monkeypatch):
        monkeypatch.setenv("SOME_INT_VAR", "42")
        assert _env_int("SOME_INT_VAR", 7) == 42

    def test_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("SOME_INT_VAR", "not-a-number")
        assert _env_int("SOME_INT_VAR", 7) == 7


class TestRegistry:
    def test_env_configuration(self, monkeypatch):
        monkeypatch.setenv("LMER_SLACK_CHAT_IDLE_TIMEOUT_MINUTES", "10")
        monkeypatch.setenv("LMER_SLACK_CHAT_MAX_SESSIONS", "2")
        monkeypatch.setenv("LMER_SLACK_CHAT_BIN", "/usr/local/bin/lmer")
        mgr = SessionManager()
        assert mgr.idle_timeout_minutes == 10
        assert mgr.max_sessions == 2
        assert mgr.lmer_bin == "/usr/local/bin/lmer"

    def test_env_cwd_and_log_dir(self, monkeypatch):
        monkeypatch.setenv("LMER_SLACK_CHAT_CWD", "/tmp/custom-cwd")
        monkeypatch.setenv("LMER_SLACK_CHAT_LOG_DIR", "/tmp/custom-logs")
        mgr = SessionManager()
        assert mgr.spawn_cwd == Path("/tmp/custom-cwd")
        assert mgr.log_dir == Path("/tmp/custom-logs")

    def test_lmer_env_file_from_env(self, monkeypatch):
        """LMER_SLACK_CHAT_ENV_FILE configures the forwarded --env-file path."""
        monkeypatch.setenv("LMER_SLACK_CHAT_ENV_FILE", "/etc/lmer/deploy.env")
        mgr = SessionManager()
        assert mgr.lmer_env_file == "/etc/lmer/deploy.env"

    def test_lmer_env_file_arg_overrides_env(self, monkeypatch):
        """An explicit lmer_env_file arg wins over LMER_SLACK_CHAT_ENV_FILE."""
        monkeypatch.setenv("LMER_SLACK_CHAT_ENV_FILE", "/etc/lmer/deploy.env")
        mgr = SessionManager(lmer_env_file="/custom/x.env")
        assert mgr.lmer_env_file == "/custom/x.env"

    def test_lmer_env_file_default_none(self, monkeypatch):
        """With neither arg nor env var, lmer_env_file is None (no --env-file)."""
        monkeypatch.delenv("LMER_SLACK_CHAT_ENV_FILE", raising=False)
        mgr = SessionManager()
        assert mgr.lmer_env_file is None

    def test_get_active_and_tracked(self):
        mgr = SessionManager(idle_timeout_minutes=30, max_sessions=5)
        session = _fake_session()
        mgr._sessions[session.key] = session

        assert mgr.get("C1", "111.222") is session
        assert mgr.get_active("C1", "111.222") is session
        assert mgr.is_tracked("C1", "111.222")
        assert not mgr.is_tracked("C1", "999.999")

        # A dead process is not "active"
        session.process.returncode = 0
        assert mgr.get_active("C1", "111.222") is None
        assert not mgr.is_tracked("C1", "111.222")

    def test_touch_resets_idle_timer(self):
        mgr = SessionManager(idle_timeout_minutes=30, max_sessions=5)
        session = _fake_session()
        mgr._sessions[session.key] = session
        session.last_activity = time.monotonic() - 1000

        assert session.idle_seconds() > 900
        assert mgr.touch("C1", "111.222") is True
        assert session.idle_seconds() < 5

    def test_touch_untracked_returns_false(self):
        mgr = SessionManager(idle_timeout_minutes=30, max_sessions=5)
        assert mgr.touch("C1", "111.222") is False

    def test_get_active_in_channel(self):
        mgr = SessionManager(idle_timeout_minutes=30, max_sessions=5)
        dead = _fake_session(channel="D1", thread_ts="1.1", returncode=0)
        live = _fake_session(channel="D1", thread_ts="2.2")
        other = _fake_session(channel="D2", thread_ts="3.3")
        for s in (dead, live, other):
            mgr._sessions[s.key] = s

        assert mgr.get_active_in_channel("D1") is live
        assert mgr.get_active_in_channel("D2") is other
        assert mgr.get_active_in_channel("D3") is None

    def test_capacity_counts_only_running(self):
        mgr = SessionManager(idle_timeout_minutes=30, max_sessions=2)
        live = _fake_session(thread_ts="1.1")
        dead = _fake_session(thread_ts="2.2", returncode=0)
        mgr._sessions[live.key] = live
        mgr._sessions[dead.key] = dead

        assert mgr.active_count() == 1
        assert not mgr.at_capacity()

        live2 = _fake_session(thread_ts="3.3")
        mgr._sessions[live2.key] = live2
        assert mgr.at_capacity()


class TestSpawnAndTerminate:
    @pytest.fixture
    def fake_lmer(self, tmp_path: Path) -> str:
        """A stand-in for the lmer binary that just sleeps."""
        script = tmp_path / "fake-lmer"
        script.write_text("#!/bin/bash\nsleep 60\n")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return str(script)

    @pytest.fixture
    def manager(self, fake_lmer: str, tmp_path: Path) -> SessionManager:
        return SessionManager(
            idle_timeout_minutes=30,
            max_sessions=2,
            lmer_bin=fake_lmer,
            spawn_cwd=str(tmp_path / "cwd"),
            log_dir=str(tmp_path / "logs"),
        )

    @pytest.mark.asyncio
    async def test_spawn_and_terminate(self, manager: SessionManager):
        permalink = "https://x.slack.com/archives/C1/p1112220000000000"
        session = await manager.spawn("C1", "111.222", permalink)
        try:
            assert session.is_running
            assert manager.is_tracked("C1", "111.222")
            assert manager.active_count() == 1
            assert session.log_path.parent.is_dir()
        finally:
            await manager.terminate(session)

        assert not session.is_running
        assert manager.get("C1", "111.222") is None

    @pytest.mark.asyncio
    async def test_spawn_forwards_env_file(self, tmp_path: Path):
        """When lmer_env_file is set, spawn passes '--env-file <path>' before
        the 'chat' subcommand to the spawned lmer process (issue #75)."""
        argv_log = tmp_path / "argv.txt"
        script = _write_recording_lmer(tmp_path, argv_log)
        env_file = tmp_path / "deploy.env"
        env_file.write_text("GITLAB_TOKEN_example_com=glpat-fixturetoken\n")

        manager = SessionManager(
            idle_timeout_minutes=30,
            max_sessions=2,
            lmer_bin=str(script),
            spawn_cwd=str(tmp_path / "cwd"),
            log_dir=str(tmp_path / "logs"),
            lmer_env_file=str(env_file),
        )
        permalink = "https://x.slack.com/archives/C1/p1112220000000000"
        session = await manager.spawn("C1", "111.222", permalink)
        try:
            args = await _read_recorded_argv(argv_log)
            assert "--env-file" in args, f"args={args}"
            idx = args.index("--env-file")
            assert args[idx + 1] == str(env_file)
            assert idx < args.index("chat"), "--env-file must precede 'chat'"
            assert args[-2:] == ["chat", permalink]
        finally:
            await manager.terminate(session)

    @pytest.mark.asyncio
    async def test_spawn_omits_env_file_when_unset(
        self, tmp_path: Path, monkeypatch
    ):
        """With no lmer_env_file (and no LMER_SLACK_CHAT_ENV_FILE), spawn passes
        only 'chat <permalink>' — spawning is unchanged."""
        monkeypatch.delenv("LMER_SLACK_CHAT_ENV_FILE", raising=False)
        argv_log = tmp_path / "argv.txt"
        script = _write_recording_lmer(tmp_path, argv_log)

        manager = SessionManager(
            idle_timeout_minutes=30,
            max_sessions=2,
            lmer_bin=str(script),
            spawn_cwd=str(tmp_path / "cwd"),
            log_dir=str(tmp_path / "logs"),
        )
        permalink = "https://x.slack.com/archives/C1/p1112220000000000"
        session = await manager.spawn("C1", "111.222", permalink)
        try:
            args = await _read_recorded_argv(argv_log)
            assert "--env-file" not in args
            assert args == ["chat", permalink]
        finally:
            await manager.terminate(session)

    @pytest.mark.asyncio
    async def test_spawn_rejects_duplicate_thread(self, manager: SessionManager):
        permalink = "https://x.slack.com/archives/C1/p1112220000000000"
        session = await manager.spawn("C1", "111.222", permalink)
        try:
            with pytest.raises(RuntimeError, match="already active"):
                await manager.spawn("C1", "111.222", permalink)
        finally:
            await manager.terminate(session)

    @pytest.mark.asyncio
    async def test_spawn_rejects_at_capacity(self, manager: SessionManager):
        manager.max_sessions = 1
        permalink = "https://x.slack.com/archives/C1/p1112220000000000"
        session = await manager.spawn("C1", "111.222", permalink)
        try:
            with pytest.raises(RuntimeError, match="limit"):
                await manager.spawn("C1", "333.444", permalink)
        finally:
            await manager.terminate(session)

    @pytest.mark.asyncio
    async def test_terminate_escalates_past_ignored_ctrl_c(
        self, tmp_path: Path, monkeypatch
    ):
        """A child that ignores SIGINT is still killed via the signal ladder."""
        import slack_chat.sessions as sessions_mod

        # Keep the test fast: escalate after half a second instead of 10.
        monkeypatch.setattr(sessions_mod, "SHUTDOWN_GRACE_SECONDS", 0.5)

        script = tmp_path / "stubborn-lmer"
        script.write_text("#!/bin/bash\ntrap '' INT\nsleep 60 &\nwait\n")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)

        manager = SessionManager(
            idle_timeout_minutes=30,
            max_sessions=2,
            lmer_bin=str(script),
            spawn_cwd=str(tmp_path / "cwd"),
            log_dir=str(tmp_path / "logs"),
        )
        permalink = "https://x.slack.com/archives/C1/p1112220000000000"
        session = await manager.spawn("C1", "111.222", permalink)
        assert session.is_running

        await asyncio.wait_for(manager.terminate(session), timeout=10)

        assert not session.is_running
        assert manager.get("C1", "111.222") is None


class TestReap:
    @pytest.mark.asyncio
    async def test_reap_drops_exited_sessions(self):
        mgr = SessionManager(idle_timeout_minutes=30, max_sessions=5)
        dead = _fake_session(returncode=0)
        mgr._sessions[dead.key] = dead

        await mgr.reap()
        assert mgr.get(*dead.key) is None

    @pytest.mark.asyncio
    async def test_reap_reports_crashed_sessions(self):
        """A nonzero exit triggers on_crash; a clean exit stays silent."""
        mgr = SessionManager(idle_timeout_minutes=30, max_sessions=5)
        crashed = _fake_session(thread_ts="1.1", returncode=1)
        clean = _fake_session(thread_ts="2.2", returncode=0)
        mgr._sessions[crashed.key] = crashed
        mgr._sessions[clean.key] = clean

        on_crash = AsyncMock()
        await mgr.reap(on_crash=on_crash)

        on_crash.assert_awaited_once_with(crashed)
        assert mgr.get(*crashed.key) is None
        assert mgr.get(*clean.key) is None

    @pytest.mark.asyncio
    async def test_reap_self_exit_reconciles_drain(self, monkeypatch):
        """A self-exited session is routed through the same drain reconcile as
        terminate(), so the executor thread and master_fd can't leak when a
        grandchild keeps the slave PTY open past the lmer parent's exit."""
        mgr = SessionManager(idle_timeout_minutes=30, max_sessions=5)
        dead = _fake_session(returncode=0)
        mgr._sessions[dead.key] = dead

        reconcile = AsyncMock()
        monkeypatch.setattr(mgr, "_reconcile_drain", reconcile)

        await mgr.reap()

        reconcile.assert_awaited_once_with(dead)
        assert mgr.get(*dead.key) is None

    @pytest.mark.asyncio
    async def test_reconcile_drain_awaits_completed_task(self):
        """_reconcile_drain awaits a drain task that unwinds on its own."""
        mgr = SessionManager(idle_timeout_minutes=30, max_sessions=5)
        session = _fake_session()

        async def _drain():
            return None

        session.drain_task = asyncio.create_task(_drain())
        await mgr._reconcile_drain(session)
        assert session.drain_task.done()
        assert not session.drain_task.cancelled()

    @pytest.mark.asyncio
    async def test_reconcile_drain_cancels_stuck_task(self, monkeypatch):
        """If the drain task does not unwind within the grace window, the
        timeout backstop cancels it rather than leaking the thread/fd."""
        import slack_chat.sessions as sessions_mod

        monkeypatch.setattr(sessions_mod, "SHUTDOWN_GRACE_SECONDS", 0.01)
        mgr = SessionManager(idle_timeout_minutes=30, max_sessions=5)
        session = _fake_session()

        async def _stuck():
            await asyncio.sleep(10)

        session.drain_task = asyncio.create_task(_stuck())
        await mgr._reconcile_drain(session)
        # cancel() was requested on timeout; awaiting now surfaces the
        # cancellation rather than leaving the task running forever.
        with pytest.raises(asyncio.CancelledError):
            await session.drain_task

    @pytest.mark.asyncio
    async def test_reap_disconnects_idle_sessions(self, monkeypatch):
        mgr = SessionManager(idle_timeout_minutes=30, max_sessions=5)
        session = _fake_session()
        mgr._sessions[session.key] = session
        session.last_activity = time.monotonic() - (31 * 60)

        terminated = []

        async def fake_terminate(s):
            terminated.append(s)
            mgr._sessions.pop(s.key, None)

        monkeypatch.setattr(mgr, "terminate", fake_terminate)
        callback = AsyncMock()

        await mgr.reap(on_idle_disconnect=callback)

        assert terminated == [session]
        callback.assert_awaited_once_with(session)

    @pytest.mark.asyncio
    async def test_reap_leaves_fresh_sessions_alone(self, monkeypatch):
        mgr = SessionManager(idle_timeout_minutes=30, max_sessions=5)
        session = _fake_session()
        mgr._sessions[session.key] = session

        terminate = AsyncMock()
        monkeypatch.setattr(mgr, "terminate", terminate)

        await mgr.reap(on_idle_disconnect=AsyncMock())

        terminate.assert_not_awaited()
        assert mgr.get(*session.key) is session


PERMALINK = "https://x.slack.com/archives/C1/p1112220000000000"


@pytest.fixture
def captured(monkeypatch):
    """Patch the subprocess spawn and PTY drain; capture the exec call.

    The real ``lmer`` is never launched - we only assert on how spawn()
    builds the command and environment.
    """
    import slack_chat.sessions as sessions_mod

    calls: dict = {}

    class _StubProc:
        returncode = None
        pid = 4242

        async def wait(self):
            return 0

    async def fake_exec(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return _StubProc()

    async def noop_drain(self, session):
        return None

    monkeypatch.setattr(sessions_mod.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(SessionManager, "_drain", noop_drain)
    return calls


@pytest.fixture
def spawn_manager(tmp_path: Path) -> SessionManager:
    """A SessionManager whose spawns are captured rather than executed."""
    return SessionManager(
        idle_timeout_minutes=30,
        max_sessions=5,
        lmer_bin="lmer",
        spawn_cwd=str(tmp_path / "cwd"),
        log_dir=str(tmp_path / "logs"),
    )


class TestSpawnWithPreset:
    """spawn() layers a preset's checkout/service/args onto the command and
    merges its env, without a preset producing the plain repo-less command."""

    @pytest.fixture
    def manager(self, spawn_manager: SessionManager) -> SessionManager:
        return spawn_manager

    @pytest.mark.asyncio
    async def test_no_preset_is_plain_command(self, manager, captured):
        session = await manager.spawn("C1", "1.1", PERMALINK)
        os.close(session.master_fd)

        assert list(captured["args"]) == ["lmer", "chat", PERMALINK]
        # Inherits the listener environment unchanged.
        assert captured["kwargs"]["env"] == dict(os.environ)

    @pytest.mark.asyncio
    async def test_full_preset_builds_command_and_env(
        self, manager, captured, monkeypatch
    ):
        monkeypatch.delenv("LMER_LLM_NAME", raising=False)
        preset = Preset(
            name="my_service",
            checkout="/srv/my-service",
            service="mysvc",
            env={"LMER_LLM_NAME": "opus"},
            args=["--ports", "2"],
        )
        session = await manager.spawn("C1", "1.1", PERMALINK, preset=preset)
        os.close(session.master_fd)

        assert list(captured["args"]) == [
            "lmer",
            "chat",
            PERMALINK,
            "--checkout",
            "/srv/my-service",
            "--service",
            "mysvc",
            "--ports",
            "2",
        ]
        assert captured["kwargs"]["env"]["LMER_LLM_NAME"] == "opus"

    @pytest.mark.asyncio
    async def test_checkout_only_preset_omits_service(self, manager, captured):
        preset = Preset(name="co", checkout="/co")
        session = await manager.spawn("C1", "1.1", PERMALINK, preset=preset)
        os.close(session.master_fd)

        cmd = list(captured["args"])
        assert cmd == ["lmer", "chat", PERMALINK, "--checkout", "/co"]
        assert "--service" not in cmd

    @pytest.mark.asyncio
    async def test_preset_env_overrides_inherited(self, manager, captured, monkeypatch):
        monkeypatch.setenv("LMER_LLM_NAME", "sonnet")
        preset = Preset(name="m", env={"LMER_LLM_NAME": "opus"})
        session = await manager.spawn("C1", "1.1", PERMALINK, preset=preset)
        os.close(session.master_fd)

        assert captured["kwargs"]["env"]["LMER_LLM_NAME"] == "opus"


class TestTokenPresetDisplacesListenerDefault:
    """A ``$preset:`` token replaces the listener-wide default outright
    (issue #181).

    The listener spawns ``lmer chat``, so ``LMER_CHAT_PRESET`` in its
    environment selects a preset for every session it starts. That default is
    supported, but it must not *stack* with a token-selected one: before this,
    the token's env won conflicts while the default silently filled every gap,
    leaving two presets in play under two different merge rules and nothing
    saying so.
    """

    @pytest.fixture
    def manager(self, spawn_manager: SessionManager) -> SessionManager:
        return spawn_manager

    @pytest.mark.asyncio
    async def test_token_preset_blanks_every_selector(
        self, manager, captured, monkeypatch
    ):
        """Both selectors are blanked, so the child resolves no preset of its own.

        Blank rather than absent: blank is the selector contract's "unset", and
        it is what survives the child's first-wins ``.env`` seeding.
        """
        monkeypatch.setenv("LMER_CHAT_PRESET", "listener-wide")
        monkeypatch.setenv("LMER_PRESET", "operator-choice")
        session = await manager.spawn(
            "C1", "1.1", PERMALINK, preset=Preset(name="from-token")
        )
        os.close(session.master_fd)

        child_env = captured["kwargs"]["env"]
        assert child_env["LMER_CHAT_PRESET"] == ""
        assert child_env["LMER_PRESET"] == ""
        assert select_preset_name(None, CHAT_TASK_ID, child_env) == (None, None), (
            "with a token in play the child must resolve no preset at all"
        )

    @pytest.mark.asyncio
    async def test_displacement_is_total_not_a_merge(
        self, manager, captured, monkeypatch, tmp_path
    ):
        """The default's keys are not inherited where the token's preset
        leaves them unset.

        This is the test that fails if someone later "helpfully" turns
        displacement back into a merge. The listener-wide default is a *real*
        preset in a real presets file, and the two deliberately differ in keys
        the token's preset does not set (``LMER_CHECKOUT_BRANCH``,
        ``LMER_REASONING_EFFORT``) — so an implementation that loaded the
        default and filled the gaps would visibly pass those through.
        """
        presets_file = tmp_path / "presets.json"
        presets_file.write_text(
            json.dumps(
                {
                    "listener-wide": {
                        "env": {
                            "LMER_CHECKOUT_BRANCH": "listener-branch",
                            "LMER_REASONING_EFFORT": "high",
                            "LMER_LLM_NAME": "sonnet",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("LMER_PRESETS_FILE", str(presets_file))
        monkeypatch.setenv("LMER_CHAT_PRESET", "listener-wide")
        monkeypatch.delenv("LMER_CHECKOUT_BRANCH", raising=False)
        monkeypatch.delenv("LMER_REASONING_EFFORT", raising=False)
        # The token's preset sets LLM_NAME only; the listener-wide default
        # would have contributed a branch and an effort on top of it.
        token_preset = Preset(name="from-token", env={"LMER_LLM_NAME": "opus"})
        session = await manager.spawn("C1", "1.1", PERMALINK, preset=token_preset)
        os.close(session.master_fd)

        child_env = captured["kwargs"]["env"]
        assert child_env["LMER_LLM_NAME"] == "opus", "the token's preset applies"
        assert "LMER_CHECKOUT_BRANCH" not in child_env, (
            "the displaced default must not fill a key the token's preset "
            "leaves unset — displacement is total, not a merge"
        )
        assert "LMER_REASONING_EFFORT" not in child_env
        assert child_env["LMER_CHAT_PRESET"] == "", (
            "and the child must not be able to load the default itself"
        )

    @pytest.mark.asyncio
    async def test_preset_env_may_set_a_selector_for_the_child(
        self, manager, captured, monkeypatch
    ):
        """Blanking runs before the preset's own env, so an operator who
        deliberately chains a default from a preset still gets it."""
        monkeypatch.setenv("LMER_CHAT_PRESET", "listener-wide")
        preset = Preset(name="from-token", env={"LMER_CHAT_PRESET": "chained"})
        session = await manager.spawn("C1", "1.1", PERMALINK, preset=preset)
        os.close(session.master_fd)

        assert captured["kwargs"]["env"]["LMER_CHAT_PRESET"] == "chained"

    @pytest.mark.asyncio
    async def test_displacement_survives_the_childs_env_file_seeding(
        self, manager, captured, monkeypatch, tmp_path
    ):
        """The default cannot come back through the child's ``.env`` seeding.

        The whole chain, end to end: the listener forwards its deployment
        ``.env`` as ``--env-file``, and the spawned CLI seeds its environment
        from that file first-wins (then the cwd's, then the state dir's), which
        only skips a variable that is *present*. A displacement that deleted the
        selectors would therefore be undone by the very file this spawn hands
        the child: it would resolve the default itself and both presets would
        apply. One file tier is enough to prove it — the seeding rule is the
        same at every tier.

        Regression contract: with the selector deleted rather than blanked, the
        seeding below re-supplies ``house-default`` and both assertions fail.
        """
        from lmer_cli.cli import apply_env_file_defaults

        env_file = tmp_path / "deploy.env"
        env_file.write_text("LMER_CHAT_PRESET=house-default\n", encoding="utf-8")
        # The deployment shape this regression is about: the default lives only
        # in the file the listener forwards, never in the listener's own
        # environment.
        manager.lmer_env_file = str(env_file)
        monkeypatch.delenv("LMER_CHAT_PRESET", raising=False)
        monkeypatch.delenv("LMER_PRESET", raising=False)
        session = await manager.spawn(
            "C1", "1.1", PERMALINK, preset=Preset(name="from-token")
        )
        os.close(session.master_fd)

        argv = list(captured["args"])
        assert "--env-file" in argv, f"argv={argv}"
        assert argv[argv.index("--env-file") + 1] == str(env_file), (
            "the child is handed the file the seeding below reads"
        )
        child_env = captured["kwargs"]["env"]

        # Run the child's real seeding against the environment it was handed.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(os, "environ", child_env)
            apply_env_file_defaults([("forwarded --env-file", env_file)])

        assert child_env["LMER_CHAT_PRESET"] == "", (
            "a blank selector is present, so first-wins seeding must skip it"
        )
        assert select_preset_name(None, CHAT_TASK_ID, child_env) == (None, None), (
            "the displaced default must stay displaced after the child seeds "
            "its environment from the forwarded env file"
        )

        # Control: the same file does seed an environment that lacks the key,
        # so the assertions above rest on the blanking, not on a no-op seeding.
        without_selector: dict[str, str] = {}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(os, "environ", without_selector)
            apply_env_file_defaults([("forwarded --env-file", env_file)])
        assert without_selector["LMER_CHAT_PRESET"] == "house-default"
        assert select_preset_name(None, CHAT_TASK_ID, without_selector) == (
            "house-default",
            "LMER_CHAT_PRESET",
        )

    @pytest.mark.asyncio
    async def test_no_token_keeps_the_listener_wide_default(
        self, manager, captured, monkeypatch
    ):
        """With no token the default is untouched — the child applies it."""
        monkeypatch.setenv("LMER_CHAT_PRESET", "listener-wide")
        session = await manager.spawn("C1", "1.1", PERMALINK)
        os.close(session.master_fd)

        child_env = captured["kwargs"]["env"]
        assert child_env["LMER_CHAT_PRESET"] == "listener-wide"
        assert select_preset_name(None, CHAT_TASK_ID, child_env) == (
            "listener-wide",
            "LMER_CHAT_PRESET",
        )

    @pytest.mark.asyncio
    async def test_spawn_log_names_the_displaced_default(
        self, manager, captured, monkeypatch, caplog
    ):
        """A spawn log may never hide a second preset."""
        monkeypatch.setenv("LMER_CHAT_PRESET", "listener-wide")
        with caplog.at_level("INFO", logger="lmer_slack.sessions"):
            session = await manager.spawn(
                "C1", "1.1", PERMALINK, preset=Preset(name="from-token")
            )
        os.close(session.master_fd)

        line = next(
            r.getMessage() for r in caplog.records if "lmer_session_spawned" in r.getMessage()
        )
        assert "preset=from-token" in line
        assert "displaced_default=listener-wide(LMER_CHAT_PRESET)" in line

    @pytest.mark.asyncio
    async def test_spawn_log_names_an_applying_default(
        self, manager, captured, monkeypatch, caplog
    ):
        monkeypatch.setenv("LMER_CHAT_PRESET", "listener-wide")
        with caplog.at_level("INFO", logger="lmer_slack.sessions"):
            session = await manager.spawn("C1", "1.1", PERMALINK)
        os.close(session.master_fd)

        line = next(
            r.getMessage() for r in caplog.records if "lmer_session_spawned" in r.getMessage()
        )
        assert "preset=-" in line
        assert "default_preset=listener-wide(LMER_CHAT_PRESET)" in line


class TestListenerDefaultPreset:
    """The listener-wide default resolver (issue #181)."""

    def test_scoped_var_wins_over_generic(self):
        assert listener_default_preset(
            {"LMER_CHAT_PRESET": "scoped", "LMER_PRESET": "generic"}
        ) == ("scoped", "LMER_CHAT_PRESET"), (
            "LMER_CHAT_PRESET outranking an exported LMER_PRESET is the #140 "
            "specificity rule, not a bug — pin it so it is not 'fixed' away"
        )

    def test_generic_alone_still_selects(self):
        assert listener_default_preset({"LMER_PRESET": "generic"}) == (
            "generic",
            "LMER_PRESET",
        )

    def test_nothing_selected(self):
        assert listener_default_preset({}) == (None, None)

    def test_blank_value_is_unset(self):
        assert listener_default_preset({"LMER_CHAT_PRESET": "  "}) == (None, None)


def _childs_own_default(
    environ: dict[str, str], candidates: list[tuple[str, Path]]
) -> tuple[str | None, str | None]:
    """What the spawned CLI itself resolves, run through its own real code.

    ``apply_env_file_defaults`` then ``select_preset_name`` is verbatim the
    order ``lmer`` main() uses, so a display that agrees with this agrees with
    the child (issue #259). Runs against a copied environment so the seeding —
    which writes to ``os.environ`` — cannot touch the suite's.
    """
    from lmer_cli.cli import apply_env_file_defaults

    child_env = dict(environ)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "environ", child_env)
        apply_env_file_defaults(candidates)
    return select_preset_name(None, CHAT_TASK_ID, child_env)


def _childs_own_presets(
    environ: dict[str, str], candidates: list[tuple[str, Path]]
) -> dict[str, Preset]:
    """Which presets the spawned CLI itself loads, through its own real code.

    The availability counterpart of :func:`_childs_own_default` (issue #279):
    ``apply_env_file_defaults`` then ``load_presets()`` reading the seeded
    ``LMER_PRESETS_FILE`` is verbatim what ``lmer`` main() does, so a display
    that agrees with this agrees with the child about what is defined.
    ``load_presets`` is called inside the patch context because it reads the
    environment the seeding just wrote.
    """
    from lmer_cli.cli import apply_env_file_defaults

    child_env = dict(environ)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "environ", child_env)
        apply_env_file_defaults(candidates)
        return load_presets()


class TestDefaultPresetMatchesTheChild:
    """``SessionManager.default_preset()`` reports the preset the spawned CLI
    actually resolves, ``.env`` tiers included (issue #259), and
    ``child_presets()`` reports what that same CLI would find *defined*
    (issue #279).

    Every case asserts the displayed answer twice: literally, and against
    :func:`_childs_own_default` / :func:`_childs_own_presets` — the child's own
    seeding, selection and loading code run over the same environment and the
    same candidate files. The second assertion is the point: a display that
    only *looks* right is what the bug was, and a trade of a visible ``-`` for
    a confidently wrong name would be worse than the bug.
    """

    @pytest.fixture(autouse=True)
    def isolated_state_dir(self, monkeypatch, tmp_path: Path) -> Path:
        """Point lmer's state dir at an empty directory.

        ``~/.lmer/.env`` is one of the tiers the spawned CLI seeds from, so it
        is one of the tiers the display models. A real one on the machine
        running the suite would otherwise select a preset for these tests.
        """
        from lmer_cli import runtime

        state = tmp_path / "state"
        state.mkdir()
        monkeypatch.setattr(runtime, "_LMER_STATE_DIR", state)
        return state

    @pytest.fixture
    def manager(self, tmp_path: Path) -> SessionManager:
        return SessionManager(
            idle_timeout_minutes=30,
            max_sessions=5,
            lmer_bin="lmer",
            spawn_cwd=str(tmp_path / "cwd"),
            log_dir=str(tmp_path / "logs"),
        )

    def _forward(self, manager: SessionManager, tmp_path: Path, body: str) -> Path:
        """Give *manager* a forwarded ``--env-file`` containing *body*."""
        env_file = tmp_path / "deploy.env"
        env_file.write_text(body, encoding="utf-8")
        manager.lmer_env_file = str(env_file)
        return env_file

    def _assert_agrees(
        self, manager: SessionManager, environ: dict[str, str], expected: tuple
    ) -> None:
        candidates = manager._child_env_file_candidates()
        assert manager.default_preset(environ) == expected
        assert manager.default_preset(environ) == _childs_own_default(
            environ, candidates
        )

    def _presets_file(self, tmp_path: Path, *names: str) -> Path:
        """Write a presets file defining *names* and return its path."""
        path = tmp_path / "presets.json"
        path.write_text(
            json.dumps({name: {"checkout": f"/co/{name}"} for name in names}),
            encoding="utf-8",
        )
        return path

    def _assert_presets_agree(
        self, manager: SessionManager, environ: dict[str, str], expected: set[str]
    ) -> None:
        candidates = manager._child_env_file_candidates()
        assert set(manager.child_presets(environ)) == expected
        assert set(manager.child_presets(environ)) == set(
            _childs_own_presets(environ, candidates)
        )

    def test_default_only_in_the_forwarded_file_is_named(self, manager, tmp_path):
        """The reported bug: the child loads it, so the display must name it."""
        self._forward(manager, tmp_path, "LMER_CHAT_PRESET=house-default\n")

        self._assert_agrees(manager, {}, ("house-default", "LMER_CHAT_PRESET"))

    def test_the_environment_beats_the_forwarded_file(self, manager, tmp_path):
        """First-wins: an exported selector is never overwritten by a file."""
        self._forward(manager, tmp_path, "LMER_CHAT_PRESET=house-default\n")

        self._assert_agrees(
            manager,
            {"LMER_CHAT_PRESET": "exported"},
            ("exported", "LMER_CHAT_PRESET"),
        )

    def test_no_selector_anywhere_is_still_nothing(self, manager, tmp_path):
        self._forward(manager, tmp_path, "GITLAB_TOKEN_example_com=glpat-fixture\n")

        self._assert_agrees(manager, {}, (None, None))

    def test_scoped_selector_from_a_file_outranks_an_exported_generic(
        self, manager, tmp_path
    ):
        """Seeding happens before selection, for the child and so for us.

        The child seeds ``LMER_CHAT_PRESET`` from the file and only then picks
        the most specific selector, so the file's scoped var beats the exported
        generic one (the #140 specificity rule). Resolving the environment
        first and falling back to the file only afterwards would name
        ``generic`` here — a confidently wrong name, which is worse than the
        ``-`` this replaces.
        """
        self._forward(manager, tmp_path, "LMER_CHAT_PRESET=scoped\n")

        self._assert_agrees(
            manager, {"LMER_PRESET": "generic"}, ("scoped", "LMER_CHAT_PRESET")
        )

    def test_a_blank_selector_is_present_so_no_file_can_fill_it(
        self, manager, tmp_path
    ):
        """The displacement contract, seen from the display side.

        spawn() blanks the selectors rather than deleting them precisely
        because the child's seeding skips a key that is *present*. The display
        follows the same rule, so a displaced default is reported displaced.
        """
        self._forward(manager, tmp_path, "LMER_CHAT_PRESET=house-default\n")

        self._assert_agrees(manager, {"LMER_CHAT_PRESET": ""}, (None, None))

    def test_the_childs_own_cwd_is_a_tier_the_listener_never_sees(
        self, manager, tmp_path
    ):
        """The child's cwd is spawn_cwd, not the listener's directory, so its
        ``.env`` is a tier no listener environment can show."""
        manager.spawn_cwd.mkdir(parents=True, exist_ok=True)
        (manager.spawn_cwd / ".env").write_text(
            "LMER_CHAT_PRESET=from-spawn-cwd\n", encoding="utf-8"
        )

        self._assert_agrees(manager, {}, ("from-spawn-cwd", "LMER_CHAT_PRESET"))

    def test_the_forwarded_file_outranks_the_childs_cwd(self, manager, tmp_path):
        manager.spawn_cwd.mkdir(parents=True, exist_ok=True)
        (manager.spawn_cwd / ".env").write_text(
            "LMER_CHAT_PRESET=from-spawn-cwd\n", encoding="utf-8"
        )
        self._forward(manager, tmp_path, "LMER_CHAT_PRESET=forwarded\n")

        self._assert_agrees(manager, {}, ("forwarded", "LMER_CHAT_PRESET"))

    def test_a_forwarded_path_that_is_not_a_file_is_skipped(self, manager, tmp_path):
        """Matching the CLI, which warns and skips a missing or non-regular
        ``--env-file`` rather than failing — the display must not raise on a
        path the child merely shrugs at."""
        manager.lmer_env_file = str(tmp_path / "nowhere.env")
        assert manager.default_preset({}) == (None, None)

        a_directory = tmp_path / "adir"
        a_directory.mkdir()
        manager.lmer_env_file = str(a_directory)
        assert manager.default_preset({}) == (None, None)

    def test_an_unreadable_tier_loses_a_name_not_the_spawn(
        self, manager, tmp_path, monkeypatch
    ):
        """Reading is best-effort: a display cannot be worth failing a spawn.

        The failure is injected rather than made with ``chmod``, which proves
        nothing when the suite runs as root.
        """
        import slack_chat.sessions as sessions_mod

        self._forward(manager, tmp_path, "LMER_CHAT_PRESET=house\n")

        def unreadable(*args, **kwargs):
            raise PermissionError("nope")

        monkeypatch.setattr(sessions_mod, "dotenv_values", unreadable)
        assert manager.default_preset({}) == (None, None)

    def test_a_presets_file_only_in_the_forwarded_file_is_loaded(
        self, manager, tmp_path
    ):
        """The #279 bug, at its source: ``LMER_PRESETS_FILE`` rides the same
        tiers as the selector, so a deployment that puts both in the forwarded
        ``--env-file`` has a default that is named *and* defined."""
        presets = self._presets_file(tmp_path, "house")
        self._forward(
            manager,
            tmp_path,
            f"LMER_CHAT_PRESET=house\nLMER_PRESETS_FILE={presets}\n",
        )

        self._assert_agrees(manager, {}, ("house", "LMER_CHAT_PRESET"))
        self._assert_presets_agree(manager, {}, {"house"})

    def test_a_presets_file_in_the_environment_still_loads(self, manager, tmp_path):
        """The ordinary deployment, unchanged: the listener exports the path."""
        presets = self._presets_file(tmp_path, "house", "other")
        self._forward(manager, tmp_path, "LMER_CHAT_PRESET=house\n")

        environ = {"LMER_PRESETS_FILE": str(presets)}
        self._assert_agrees(manager, environ, ("house", "LMER_CHAT_PRESET"))
        self._assert_presets_agree(manager, environ, {"house", "other"})

    def test_the_environment_beats_the_forwarded_presets_file(
        self, manager, tmp_path
    ):
        """First-wins applies to this key like any other: an exported path is
        the one the child loads, so it is the one the display loads."""
        exported = self._presets_file(tmp_path, "exported")
        forwarded = tmp_path / "forwarded-presets.json"
        forwarded.write_text(
            json.dumps({"forwarded": {"checkout": "/co"}}), encoding="utf-8"
        )
        self._forward(manager, tmp_path, f"LMER_PRESETS_FILE={forwarded}\n")

        self._assert_presets_agree(
            manager, {"LMER_PRESETS_FILE": str(exported)}, {"exported"}
        )

    def test_a_presets_file_the_child_cannot_read_defines_nothing(
        self, manager, tmp_path
    ):
        """The warning direction stays available: a name resolves, but the file
        it would be defined in is missing, so the child finds nothing either and
        the session really will fail to start."""
        self._forward(
            manager,
            tmp_path,
            f"LMER_CHAT_PRESET=house\nLMER_PRESETS_FILE={tmp_path / 'gone.json'}\n",
        )

        self._assert_agrees(manager, {}, ("house", "LMER_CHAT_PRESET"))
        self._assert_presets_agree(manager, {}, set())

    def test_a_malformed_presets_file_defines_nothing(self, manager, tmp_path):
        """Loading is forgiving in the same place the child's is — unparseable
        is empty, not an exception thrown at a spawn this only annotates."""
        broken = tmp_path / "presets.json"
        broken.write_text("{not json", encoding="utf-8")
        self._forward(manager, tmp_path, f"LMER_PRESETS_FILE={broken}\n")

        self._assert_presets_agree(manager, {}, set())

    def test_no_presets_file_anywhere_defines_nothing(self, manager, tmp_path):
        self._forward(manager, tmp_path, "LMER_CHAT_PRESET=house\n")

        self._assert_presets_agree(manager, {}, set())

    def test_the_childs_own_cwd_is_a_presets_tier_too(self, manager, tmp_path):
        """Same tier list as the selectors, proven on the one tier the listener
        can never see from its own environment."""
        presets = self._presets_file(tmp_path, "from-spawn-cwd")
        manager.spawn_cwd.mkdir(parents=True, exist_ok=True)
        (manager.spawn_cwd / ".env").write_text(
            f"LMER_PRESETS_FILE={presets}\n", encoding="utf-8"
        )

        self._assert_presets_agree(manager, {}, {"from-spawn-cwd"})


class TestSpawnLogNamesFileSourcedDefaults:
    """The spawn log's preset fields see the forwarded env file too (#259)."""

    @pytest.fixture(autouse=True)
    def isolated_state_dir(self, monkeypatch, tmp_path: Path) -> Path:
        from lmer_cli import runtime

        state = tmp_path / "state"
        state.mkdir()
        monkeypatch.setattr(runtime, "_LMER_STATE_DIR", state)
        return state

    @pytest.fixture
    def manager(self, spawn_manager: SessionManager, tmp_path: Path) -> SessionManager:
        env_file = tmp_path / "deploy.env"
        env_file.write_text("LMER_CHAT_PRESET=house-default\n", encoding="utf-8")
        spawn_manager.lmer_env_file = str(env_file)
        return spawn_manager

    @pytest.fixture(autouse=True)
    def no_selectors_in_the_environment(self, monkeypatch):
        """The deployment shape the bug is about: the default lives only in
        the file the listener forwards."""
        monkeypatch.delenv("LMER_CHAT_PRESET", raising=False)
        monkeypatch.delenv("LMER_PRESET", raising=False)

    @pytest.mark.asyncio
    async def test_applying_default_from_the_file_is_named(
        self, manager, captured, caplog
    ):
        with caplog.at_level("INFO", logger="lmer_slack.sessions"):
            session = await manager.spawn("C1", "1.1", PERMALINK)
        os.close(session.master_fd)

        line = next(
            r.getMessage()
            for r in caplog.records
            if "lmer_session_spawned" in r.getMessage()
        )
        assert "default_preset=house-default(LMER_CHAT_PRESET)" in line, (
            "the child loads this default from the forwarded file, so a log "
            "line reading `default_preset=-` reports a session that never ran"
        )

    @pytest.mark.asyncio
    async def test_displaced_default_from_the_file_is_named(
        self, manager, captured, caplog
    ):
        with caplog.at_level("INFO", logger="lmer_slack.sessions"):
            session = await manager.spawn(
                "C1", "1.1", PERMALINK, preset=Preset(name="from-token")
            )
        os.close(session.master_fd)

        line = next(
            r.getMessage()
            for r in caplog.records
            if "lmer_session_spawned" in r.getMessage()
        )
        assert "preset=from-token" in line
        assert "displaced_default=house-default(LMER_CHAT_PRESET)" in line
        assert captured["kwargs"]["env"]["LMER_CHAT_PRESET"] == "", (
            "and it really is displaced: blanking keeps the child's own "
            "seeding from putting it back"
        )
