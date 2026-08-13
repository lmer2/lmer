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
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from lmer_cli.presets import (
    PRESETS_FILE_ENV,
    Preset,
    load_presets,
    preset_selector_vars,
    select_preset_name,
)

logger = logging.getLogger("lmer_slack.sessions")

# The taskdef every spawned session runs: the listener's command is always
# `lmer chat <permalink>`. Named because it is also what decides which
# taskdef-scoped env var can select a preset for those sessions
# (`LMER_CHAT_PRESET`, issue #140) — the command and the selector must stay
# the same taskdef or the listener would strip the wrong variable (#181).
CHAT_TASK_ID = "chat"

# How often the reaper loop wakes up to check sessions, in seconds.
REAP_INTERVAL_SECONDS = 15.0

# Grace period between shutdown escalation steps, in seconds.
SHUTDOWN_GRACE_SECONDS = 10.0


def _kv(**fields) -> str:
    """Render ``key=value`` pairs for a structured-ish stdlib log line."""
    return " ".join(f"{k}={v}" for k, v in fields.items())


def listener_default_preset(
    environ: Mapping[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Return the ``(name, source)`` of the listener-wide default preset.

    Every session the listener spawns is an ``lmer chat`` invocation
    inheriting the listener's environment, so ``LMER_CHAT_PRESET`` (and,
    failing that, ``LMER_PRESET``) selects a preset for all of them — the
    taskdef-scoped selector from issue #140 landing on the one taskdef this
    spawner ever runs. That is a supported listener-wide default, not an
    accident, but it is only honest if callers can *name* it: the ack tells
    the Slack user which preset their session actually got, and the spawn log
    records the one a ``$preset:`` token displaced (issue #181).

    Returns ``(None, None)`` when no variable selects anything. Resolving the
    name against the presets file is the caller's job — an undefined name is
    still "what is selected".

    Reads *environ* and nothing else. The spawned CLI also seeds itself from
    ``.env`` files, which is a tier no environment mapping shows;
    :meth:`SessionManager.default_preset` builds the mapping that includes it
    (issue #259) and is what callers with a manager in hand should use.
    """
    return select_preset_name(None, CHAT_TASK_ID, environ)


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
        lmer_env_file: Optional path to a .env file forwarded to each spawned
            ``lmer chat`` as ``--env-file`` so its variables (git tokens,
            ``LMER_*`` settings, ...) reach the chat container even though the
            spawn cwd has no .env (issue #75). Defaults to
            ``LMER_SLACK_CHAT_ENV_FILE``; when neither is set no ``--env-file``
            is passed and lmer falls back to its usual cwd/.env + ~/.lmer/.env
            loading.
    """

    def __init__(
        self,
        idle_timeout_minutes: int | None = None,
        max_sessions: int | None = None,
        lmer_bin: str | None = None,
        spawn_cwd: str | None = None,
        log_dir: str | None = None,
        lmer_env_file: str | None = None,
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
        self.lmer_env_file = lmer_env_file or os.getenv("LMER_SLACK_CHAT_ENV_FILE")
        self._sessions: dict[tuple[str, str], Session] = {}

    # ------------------------------------------------------------------
    # Preset resolution
    # ------------------------------------------------------------------

    def _child_env_file_candidates(self) -> list[tuple[str, Path]]:
        """The ``.env`` files the spawned CLI seeds itself from, in its order.

        Mirrors what ``lmer`` builds for a spawn from here: the forwarded
        ``--env-file`` first (gated on ``is_file()``, because a path that
        exists but is not a regular file is warned about and skipped there
        too), then the CLI's own defaults resolved against the *child's* cwd —
        ``spawn_cwd``, not the listener's directory. The default tiers come
        from the CLI's own builder so the list here cannot drift from the list
        the child actually reads.
        """
        # Imported lazily: the CLI module is the whole lmer entry point, and
        # the listener should not pay for it just to import this one.
        from lmer_cli.cli import default_env_file_candidates

        candidates: list[tuple[str, Path]] = []
        if self.lmer_env_file:
            explicit = Path(self.lmer_env_file).expanduser()
            if explicit.is_file():
                candidates.append(("--env-file", explicit))
        return candidates + default_env_file_candidates(cwd=self.spawn_cwd)

    def _seeded_child_env(
        self, names: list[str], environ: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        """Return *environ* with *names* seeded the way the spawned CLI seeds.

        Stage one of the child's startup, modeled: for each candidate file in
        the child's order, take a key only when it is *absent* from the
        environment (a present-but-blank key is not seeded, which is what makes
        the displacement in :meth:`spawn` hold) and its parsed value is not
        ``None``. First-wins, so the process environment outranks every file
        and earlier files outrank later ones — ``apply_env_file_defaults``'
        rule, applied to a copy instead of to ``os.environ``.

        Every variable the display has to reason about goes through here, so
        "what the child would read" is decided in one place: the selectors for
        :meth:`default_preset` (issue #259) and ``LMER_PRESETS_FILE`` for
        :meth:`child_presets` (issue #279). Seeding is per key, so asking for
        one name or for all of them gives each name the same value.

        The one thing this cannot reproduce is a value that interpolates
        ``${VAR}``: ``dotenv_values`` expands against the live ``os.environ``,
        and the child expands against an ``os.environ`` that earlier tiers have
        already seeded. Only the lower tiers can differ, and only for a
        reference to a key an upper tier introduced.
        """
        env = dict(os.environ if environ is None else environ)
        for _location, env_file in self._child_env_file_candidates():
            unseeded = [name for name in names if name not in env]
            if not unseeded:
                break
            try:
                if not env_file.exists():
                    continue
                values = dotenv_values(dotenv_path=str(env_file))
            except OSError:
                # A tier that cannot be read cannot be modeled; skipping it
                # costs at most a name in a log line, while raising would cost
                # the spawn this resolution only annotates.
                continue
            for var in unseeded:
                if values.get(var) is not None:
                    env[var] = values[var]
        return env

    def default_preset(
        self, environ: Mapping[str, str] | None = None
    ) -> tuple[str | None, str | None]:
        """Return the ``(name, source)`` of the default preset the child gets.

        :func:`listener_default_preset` sees only an environment mapping, but
        the spawned CLI resolves its preset *after* seeding that environment
        from ``.env`` files — so a default living only in the forwarded
        ``--env-file`` used to be applied by the child and reported here as
        "none" (issue #259). This reproduces the child's own two-stage
        resolution instead: :meth:`_seeded_child_env` for the selectors, then
        the selector precedence over the seeded mapping, which is the order the
        child evaluates in too — ``LMER_CHAT_PRESET`` before ``LMER_PRESET``,
        both after every file tier. Doing it in this order is what makes a
        scoped selector from a file outrank a generic one already exported,
        exactly as it does for the child; resolving per tier instead would name
        the wrong preset.

        Names only. Whether the name is *defined* is :meth:`child_presets`,
        which has to model the same tiers to answer honestly.
        """
        return listener_default_preset(
            self._seeded_child_env(preset_selector_vars(CHAT_TASK_ID), environ)
        )

    def child_presets(
        self, environ: Mapping[str, str] | None = None
    ) -> dict[str, Preset]:
        """Return the presets the spawned CLI would load, by its own tiers.

        The availability half of :meth:`default_preset`, and it has to travel
        the same road: ``LMER_PRESETS_FILE`` reaches the child through exactly
        the tiers the selectors do — the forwarded ``--env-file`` included —
        and the child loads its presets only after seeding from them. The
        listener's own module-level presets come from the listener's
        environment, which never reads that forwarded file, so a deployment
        putting *both* the selector and the presets file there had its default
        named correctly and then declared undefined: a confidently wrong "this
        session will fail to start" about a session that starts fine (issue
        #279).

        Loading is forgiving exactly where the child's is: an unset, missing or
        malformed file yields no presets. The warning that follows from that is
        then true — the child finds nothing there either.
        """
        env = self._seeded_child_env([PRESETS_FILE_ENV], environ)
        return load_presets(env.get(PRESETS_FILE_ENV, ""))

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

    async def spawn(
        self,
        channel: str,
        thread_ts: str,
        permalink: str,
        preset: Preset | None = None,
    ) -> Session:
        """Spawn an ``lmer chat`` process attached to a Slack thread.

        The process inherits the listener's full environment, so host-side
        lmer configuration is visible to the spawned CLI. Values that must
        also reach *inside* the chat container, however, only do so if lmer
        forwards them — the explicitly-allowlisted ``LMER_*`` keys, plus
        anything in a forwarded ``.env``. When ``lmer_env_file`` is set it is
        passed as ``lmer --env-file`` so that file's variables (git tokens,
        ``LMER_*`` settings, ...) are forwarded into the container even though
        the spawn cwd has no ``.env`` of its own (issue #75).

        When *preset* is given, its operator-defined startup configuration is
        layered on: ``--checkout``/``--service`` flags and any extra ``args``
        are appended to the command, and its ``env`` is merged over the
        inherited environment (the preset wins on conflict).

        A token-selected *preset* also **displaces** any listener-wide default
        (see :func:`listener_default_preset`) rather than stacking with it:
        every preset selector is set to the empty string in the child's
        environment, so the spawned CLI resolves no preset of its own. Blank is
        the selector contract's "unset" (see
        :func:`~lmer_cli.presets.select_preset_name`), and it is blank rather
        than absent because the child seeds its environment first-wins from
        ``.env`` files — the forwarded ``--env-file`` included. An absent key is
        one a file may re-supply, which would hand the child the very default
        this displaces; a present-but-blank key is not seeded at any file tier
        of the spawned CLI's own environment. The container environment that
        CLI then builds merges the forwarded ``--env-file`` under different
        rules and can still carry the selector — a tier this displacement does
        not reach, and safe only while nothing inside the container reads the
        selectors (see issue #259 for the family of blind spots).
        Displacement is total by construction — the default is never loaded, so
        none of its values survive and none of its keys are inherited where the
        token's preset leaves them unset (issue #181). Without this the two
        merged under two different rules: the token's env won conflicts while
        the default silently filled every gap.

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

        # Base command + inherited environment, then layer the preset on top.
        env = dict(os.environ)
        cmd: list[str] = [self.lmer_bin]
        # Global lmer flags precede the `chat` subcommand. Forward an explicit
        # .env into the chat container when configured, so vars that live only
        # in the listener's deployment dir reach lmer even though the spawn cwd
        # is a scratch dir with no .env of its own (issue #75).
        if self.lmer_env_file:
            cmd += ["--env-file", self.lmer_env_file]
        cmd += [CHAT_TASK_ID, permalink]
        displaced_name, displaced_source = (None, None)
        if preset is not None:
            # Whatever the child would have selected is displaced, so read it
            # before blanking — the spawn log names it.
            displaced_name, displaced_source = self.default_preset(env)
            # Blank, not absent: the child seeds its environment from .env
            # files first-wins, so deleting a selector only invites a file to
            # put the default back. Blanking runs before the preset's own env,
            # so a preset that deliberately sets a selector (an operator
            # chaining a default) still takes effect.
            for var in preset_selector_vars(CHAT_TASK_ID):
                env[var] = ""
            # Preset options are `chat` subcommand flags / args, appended after.
            cmd += preset.cli_tokens()
            env.update(preset.env)

        # Spawn under a PTY: lmer only allocates an interactive container
        # terminal (-it) when its own stdin is a TTY, and the claude
        # instance inside needs one. Nobody types into it - it exists so
        # the interactive (non-headless) claude session runs normally.
        master_fd, slave_fd = pty.openpty()
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=str(self.spawn_cwd),
                env=env,
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

        # Name every preset in effect so a spawn log can never hide one
        # (issue #181): the token-selected preset, plus the listener-wide
        # default — logged as `displaced_default` when a token replaced it, and
        # as `default_preset` when it is the one actually applying. Both are
        # resolved the way the child resolves, forwarded `.env` included, so
        # the line reports the preset the session really got (issue #259).
        if preset is not None:
            default_key = "displaced_default"
            default_name, default_source = displaced_name, displaced_source
        else:
            default_key = "default_preset"
            default_name, default_source = self.default_preset(env)
        fields = {
            "channel": channel,
            "thread_ts": thread_ts,
            "pid": process.pid,
            "permalink": permalink,
            "preset": preset.name if preset else "-",
            default_key: (
                f"{default_name}({default_source})" if default_name else "-"
            ),
            "log_path": log_path,
            "active_sessions": self.active_count(),
        }
        logger.info("lmer_session_spawned %s", _kv(**fields))
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
