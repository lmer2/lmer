"""lmer chat session management for the host-side Slack listener.

Conversational Slack threads are served by real ``lmer chat`` processes
(one per thread) spawned on the host by :mod:`slack_chat.listener`. This
module owns the lifecycle of those processes:

- Spawning ``lmer chat <thread-permalink>`` as a host-side subprocess.
- Tracking one session per ``(channel, thread_ts)`` pair.
- Resetting an idle timer on any activity in a tracked thread (human
  messages and the agent's own posts both count - the spawned agent is
  expected to post progress notes during long autonomous work).
- Reaping sessions whose process exited, and disconnecting sessions whose
  thread has been silent for longer than the idle timeout.

The lmer process is spawned under a dedicated PTY. lmer only passes
``-it`` to ``docker``/``podman`` when its stdin is a TTY, and the
interactive claude instance inside the container expects a terminal; the
PTY gives it one even though nobody is attached to it. The PTY master is
drained continuously into a per-session log file so the child never blocks
on a full terminal buffer.

Note that the claude instance is interactive but *unattended*: nothing is
ever typed into its terminal. All human interaction happens in the Slack
thread via the ``lmer-slack`` CLI inside the session. Shutdown is therefore
performed by writing Ctrl-C twice to the PTY (claude's quit chord) before
escalating to signals.
"""

import asyncio
import errno
import logging
import os
import pty
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("lmer_slack.sessions")

# How often the reaper loop wakes up to check sessions, in seconds.
REAP_INTERVAL_SECONDS = 15.0

# Grace period between shutdown escalation steps, in seconds.
SHUTDOWN_GRACE_SECONDS = 10.0


def _kv(**fields) -> str:
    """Render ``key=value`` pairs for a structured-ish stdlib log line."""
    return " ".join(f"{k}={v}" for k, v in fields.items())


def _env_int(name: str, default: int) -> int:
    """Read an integer env var, falling back to *default* on bad values."""
    raw = os.getenv(name, "")
    try:
        return int(raw) if raw.strip() else default
    except ValueError:
        logger.warning("invalid_int_env_var %s", _kv(name=name, value=raw, default=default))
        return default


@dataclass
class Session:
    """A single running lmer chat session attached to a Slack thread."""

    channel: str
    thread_ts: str
    permalink: str
    process: asyncio.subprocess.Process
    master_fd: int
    log_path: Path
    started_at: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    drain_task: asyncio.Task | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.channel, self.thread_ts)

    @property
    def is_running(self) -> bool:
        return self.process.returncode is None

    def touch(self) -> None:
        """Reset the idle timer."""
        self.last_activity = time.monotonic()

    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_activity


class SessionManager:
    """Registry and lifecycle manager for lmer chat sessions.

    Configuration env vars use the ``LMER_SLACK_CHAT_*`` namespace: spawned
    sessions inherit the listener's full environment, so the per-thread
    targeting vars the lmer CLI sets inside the container
    (``LMER_SLACK_CHANNEL`` / ``LMER_SLACK_THREAD_TS`` /
    ``LMER_SLACK_PERMALINK``) stay distinct from the listener's host-side
    configuration.

    Args:
        idle_timeout_minutes: Minutes of total thread silence (no human or
            agent messages) before a session is disconnected.
        max_sessions: Maximum number of concurrently running sessions.
        lmer_bin: Executable used to spawn sessions (default ``lmer``).
        spawn_cwd: Working directory for spawned processes. Must NOT be a
            git checkout - lmer infers a repo target from a git cwd, and a
            chat session should start repo-less so the agent can resolve
            and clone a workspace from the conversation. Defaults to a
            dedicated directory under ``/tmp``.
        log_dir: Directory for per-session terminal logs.
    """

    def __init__(
        self,
        idle_timeout_minutes: int | None = None,
        max_sessions: int | None = None,
        lmer_bin: str | None = None,
        spawn_cwd: str | None = None,
        log_dir: str | None = None,
    ):
        self.idle_timeout_minutes = (
            idle_timeout_minutes
            if idle_timeout_minutes is not None
            else _env_int("LMER_SLACK_CHAT_IDLE_TIMEOUT_MINUTES", 300)
        )
        self.max_sessions = (
            max_sessions
            if max_sessions is not None
            else _env_int("LMER_SLACK_CHAT_MAX_SESSIONS", 5)
        )
        self.lmer_bin = lmer_bin or os.getenv("LMER_SLACK_CHAT_BIN", "lmer")
        self.spawn_cwd = Path(
            spawn_cwd
            or os.getenv("LMER_SLACK_CHAT_CWD", "/tmp/lmer-slack-chat-sessions")
        )
        self.log_dir = Path(
            log_dir
            or os.getenv(
                "LMER_SLACK_CHAT_LOG_DIR", "/tmp/lmer-slack-chat-sessions/logs"
            )
        )
        self._sessions: dict[tuple[str, str], Session] = {}

    # ------------------------------------------------------------------
    # Registry queries
    # ------------------------------------------------------------------

    def get(self, channel: str, thread_ts: str) -> Session | None:
        """Return the session for a thread, or None if there is none."""
        return self._sessions.get((channel, thread_ts))

    def get_active(self, channel: str, thread_ts: str) -> Session | None:
        """Return the session for a thread only if its process is running."""
        session = self.get(channel, thread_ts)
        if session and session.is_running:
            return session
        return None

    def is_tracked(self, channel: str, thread_ts: str) -> bool:
        """Whether a running session is attached to this thread."""
        return self.get_active(channel, thread_ts) is not None

    def get_active_in_channel(self, channel: str) -> Session | None:
        """Return a running session in *channel* (any thread), or None.

        Used for DMs, where conversations are effectively serial: a new
        top-level DM message should be pointed at the already-active
        session's thread instead of spawning another container.
        """
        for session in self._sessions.values():
            if session.channel == channel and session.is_running:
                return session
        return None

    def active_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.is_running)

    def at_capacity(self) -> bool:
        return self.active_count() >= self.max_sessions

    def touch(self, channel: str, thread_ts: str) -> bool:
        """Reset the idle timer for a tracked thread.

        Returns:
            True if a running session was touched, False otherwise.
        """
        session = self.get_active(channel, thread_ts)
        if session:
            session.touch()
            return True
        return False

    # ------------------------------------------------------------------
    # Spawning
    # ------------------------------------------------------------------

    async def spawn(self, channel: str, thread_ts: str, permalink: str) -> Session:
        """Spawn an ``lmer chat`` process attached to a Slack thread.

        The process inherits the listener's full environment, so lmer
        configuration (LMER_* vars, git tokens, model API keys, ...) is
        passed through from the listener's ``.env`` file automatically.

        Raises:
            RuntimeError: If a running session already exists for the
                thread or the manager is at capacity.
        """
        if self.get_active(channel, thread_ts):
            raise RuntimeError(f"Session already active for thread {thread_ts}")
        if self.at_capacity():
            raise RuntimeError(
                f"Session limit reached ({self.max_sessions} concurrent sessions)"
            )

        self.spawn_cwd.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"{channel}-{thread_ts}.log"

        # Spawn under a PTY: lmer only allocates an interactive container
        # terminal (-it) when its own stdin is a TTY, and the claude
        # instance inside needs one. Nobody types into it - it exists so
        # the interactive (non-headless) claude session runs normally.
        master_fd, slave_fd = pty.openpty()
        try:
            process = await asyncio.create_subprocess_exec(
                self.lmer_bin,
                "chat",
                permalink,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=str(self.spawn_cwd),
                env=dict(os.environ),
                start_new_session=True,
            )
        except Exception:
            os.close(master_fd)
            raise
        finally:
            # The child holds its own copy of the slave end.
            os.close(slave_fd)

        session = Session(
            channel=channel,
            thread_ts=thread_ts,
            permalink=permalink,
            process=process,
            master_fd=master_fd,
            log_path=log_path,
        )
        session.drain_task = asyncio.create_task(self._drain(session))
        self._sessions[session.key] = session

        logger.info(
            "lmer_session_spawned %s",
            _kv(
                channel=channel,
                thread_ts=thread_ts,
                pid=process.pid,
                permalink=permalink,
                log_path=log_path,
                active_sessions=self.active_count(),
            ),
        )
        return session

    async def _drain(self, session: Session) -> None:
        """Continuously drain the session's PTY master into its log file.

        Without this the child blocks as soon as the PTY buffer fills.
        The log doubles as the session's terminal transcript for
        debugging.
        """
        loop = asyncio.get_running_loop()
        try:
            with open(session.log_path, "ab", buffering=0) as log_file:
                while True:
                    try:
                        chunk = await loop.run_in_executor(
                            None, os.read, session.master_fd, 4096
                        )
                    except OSError as exc:
                        # EIO is the normal "child closed the slave end" signal.
                        if exc.errno != errno.EIO:
                            logger.warning(
                                "lmer_session_drain_error %s",
                                _kv(thread_ts=session.thread_ts, error=exc),
                            )
                        break
                    if not chunk:
                        break
                    log_file.write(chunk)
        except Exception as exc:
            logger.warning(
                "lmer_session_log_error %s",
                _kv(thread_ts=session.thread_ts, error=exc),
            )
        finally:
            try:
                os.close(session.master_fd)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def _reconcile_drain(self, session: Session) -> None:
        """Await the drain task's self-completion, closing the PTY master fd.

        The drain loop closes ``master_fd`` in its own ``finally`` once its
        ``os.read`` unwinds (EIO when the child's slave end closes, or no more
        data). We await that self-completion rather than fire-and-forget
        cancel() - cancelling cannot interrupt an ``os.read`` already blocked in
        the executor thread, and racing the fd close against that read risks
        closing an fd the thread is still reading. A timeout backstop cancels if
        the drain somehow does not unwind (e.g. a grandchild in the
        docker/podman/claude stack holding the slave PTY open keeps EIO from
        firing), so neither the executor thread nor ``master_fd`` leaks for the
        lifetime of the listener.
        """
        if not session.drain_task:
            return
        try:
            await asyncio.wait_for(
                session.drain_task, timeout=SHUTDOWN_GRACE_SECONDS
            )
        except Exception:
            session.drain_task.cancel()

    async def terminate(self, session: Session) -> None:
        """Shut a session down, gracefully first.

        Escalation ladder:
        1. Ctrl-C twice on the PTY - claude's quit chord; lets the whole
           lmer/container stack unwind normally.
        2. SIGTERM to the process group.
        3. SIGKILL to the process group.
        """
        if session.is_running:
            try:
                os.write(session.master_fd, b"\x03")
                await asyncio.sleep(0.5)
                os.write(session.master_fd, b"\x03")
            except OSError:
                pass

            try:
                await asyncio.wait_for(
                    session.process.wait(), timeout=SHUTDOWN_GRACE_SECONDS
                )
            except asyncio.TimeoutError:
                self._signal_group(session, signal.SIGTERM)
                try:
                    await asyncio.wait_for(
                        session.process.wait(), timeout=SHUTDOWN_GRACE_SECONDS
                    )
                except asyncio.TimeoutError:
                    self._signal_group(session, signal.SIGKILL)
                    await session.process.wait()

        await self._reconcile_drain(session)
        self._sessions.pop(session.key, None)
        logger.info(
            "lmer_session_terminated %s",
            _kv(
                channel=session.channel,
                thread_ts=session.thread_ts,
                returncode=session.process.returncode,
            ),
        )

    @staticmethod
    def _signal_group(session: Session, sig: signal.Signals) -> None:
        """Send a signal to the session's process group, ignoring races."""
        try:
            os.killpg(os.getpgid(session.process.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    # ------------------------------------------------------------------
    # Reaper
    # ------------------------------------------------------------------

    async def reap(self, on_idle_disconnect=None, on_crash=None) -> None:
        """Single reaper pass: drop dead sessions, disconnect idle ones.

        Args:
            on_idle_disconnect: Optional async callback
                ``(session) -> None`` invoked AFTER an idle session has
                been terminated (used to post the reconnect message in
                the thread).
            on_crash: Optional async callback ``(session) -> None`` invoked
                when a session's process exited on its own with a nonzero
                returncode (crash). A clean exit (returncode 0) is the
                agent signing off deliberately and gets no callback.
        """
        idle_cutoff = self.idle_timeout_minutes * 60

        for session in list(self._sessions.values()):
            if not session.is_running:
                # Process exited on its own (agent signed off or crashed).
                # Reconcile the drain task the same way terminate() does so the
                # executor thread and master_fd cannot leak when a grandchild
                # holds the slave PTY open past the lmer parent's exit.
                self._sessions.pop(session.key, None)
                await self._reconcile_drain(session)
                logger.info(
                    "lmer_session_exited %s",
                    _kv(
                        channel=session.channel,
                        thread_ts=session.thread_ts,
                        returncode=session.process.returncode,
                    ),
                )
                if session.process.returncode != 0 and on_crash is not None:
                    await on_crash(session)
                continue

            if session.idle_seconds() >= idle_cutoff:
                logger.info(
                    "lmer_session_idle_disconnect %s",
                    _kv(
                        channel=session.channel,
                        thread_ts=session.thread_ts,
                        idle_minutes=round(session.idle_seconds() / 60, 1),
                    ),
                )
                await self.terminate(session)
                if on_idle_disconnect is not None:
                    await on_idle_disconnect(session)

    async def run_reaper(self, on_idle_disconnect=None, on_crash=None) -> None:
        """Run the reaper loop forever (intended as a background task)."""
        while True:
            try:
                await self.reap(
                    on_idle_disconnect=on_idle_disconnect, on_crash=on_crash
                )
            except Exception as exc:
                logger.exception("lmer_session_reaper_error %s", _kv(error=exc))
            await asyncio.sleep(REAP_INTERVAL_SECONDS)

    async def shutdown_all(self) -> None:
        """Terminate every running session (listener shutdown)."""
        for session in list(self._sessions.values()):
            await self.terminate(session)
