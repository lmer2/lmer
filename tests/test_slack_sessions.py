"""Tests for slack_chat.sessions (lmer chat session lifecycle)."""

import asyncio
import os
import stat
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from lmer_cli.presets import Preset
from slack_chat.sessions import Session, SessionManager, _env_int


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


class TestSpawnWithPreset:
    """spawn() layers a preset's checkout/service/args onto the command and
    merges its env, without a preset producing the plain repo-less command."""

    @pytest.fixture
    def captured(self, monkeypatch):
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
    def manager(self, tmp_path: Path) -> SessionManager:
        return SessionManager(
            idle_timeout_minutes=30,
            max_sessions=5,
            lmer_bin="lmer",
            spawn_cwd=str(tmp_path / "cwd"),
            log_dir=str(tmp_path / "logs"),
        )

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
