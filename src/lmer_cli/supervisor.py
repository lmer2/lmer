"""
Claude Code supervisor process.

Wraps the Claude Code CLI under a PTY so a controlling process sits between
the user's terminal and claude. The supervisor:

- Allocates a PTY and spawns the wrapped command (typically ``claude``) with
  the slave end as stdin/stdout/stderr.
- Forwards bytes between the host TTY and the PTY master in both directions,
  preserving raw mode and propagating ``SIGWINCH`` for terminal resizes.
- Optionally exposes a FastAPI control plane for programmatic read/write of
  the wrapped process, plus a ``/resize`` route so a client that renders the
  session itself — a browser terminal, which has no host TTY behind it and
  therefore no ``SIGWINCH`` to follow — can declare its own geometry. The
  endpoint is gated by ``--fastapi`` (or ``LMER_FASTAPI=1``) and protected
  with a bearer token.
- Optionally injects ``/start`` followed by a CR to the wrapped process
  shortly after startup so an lmer task begins automatically. Disabled by
  ``--manual-start`` (or ``LMER_MANUAL_START=1``). CR (``\\r``) is what an
  Enter keypress produces in raw-mode TUIs like Claude Code; ``\\n`` would
  insert a literal newline into the input field without submitting. To make
  that injection robust the PTY's line-discipline echo and CR→LF translation
  are pre-cleared before fork (see ``_preconfigure_pty_for_injection``) and
  injection is deferred until Claude's input prompt has actually rendered
  (see ``_wait_for_ready_marker``) so a startup-timing race or transient
  modal/dialog can't swallow the submit CR.
- Tracks when the wrapped process last produced a byte and reports it on
  ``/healthz`` (``last_output_at`` / ``idle_seconds``). The forwarding loop
  already sees every chunk on its way to the buffer, the session log and stdout,
  so the fact costs one clock read per chunk and nothing per probe — and it is
  the only place it *can* be measured, because a finished-but-unended session is
  invisible from the host: run state flips when the session ends (spec D24), so
  until then "working" and "sitting at the prompt with nothing to do" look
  identical to everything outside the container.
- Keeps the session's own copy of everything the PTY produced, when (and only
  when) something mounted a directory at :data:`CONTAINER_SESSION_LOG_DIR` for
  it to write into. That file is the log of record for an orchestrated session:
  it is written by this process, inside the container, so it survives anything
  that happens to the host process attached to the container (see
  :class:`SessionLog`).
- Optionally injects a follow-up prompt (host CLI ``--prompt`` →
  ``LMER_START_PROMPT``) a configurable delay after the ``/start`` injection so
  an automated run can hand claude an extra instruction without manual typing.
  The gap (``--start-prompt-delay`` / ``LMER_START_PROMPT_DELAY``) lets the
  ``/start`` slash command register before the prompt is typed, so on slow
  systems the prompt does not land on the same input line as ``/start``. Tied
  to auto-start, so it is a no-op under ``--manual-start``.

The supervisor is meant to be invoked at the end of the claude-runner shell
script in place of ``exec claude``. See ``libexec/claude-runner.sh``.
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import os
import secrets
import select
import signal
import struct
import sys
import termios
import threading
import time
import tty
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Mapping, Optional

from pydantic import BaseModel, Field

from .harness import UnknownHarnessError, resolve_harness
from .util import decode_escape_bytes


DEFAULT_PORT_RANGE = (8700, 8799)
DEFAULT_AUTO_START_DELAY = 1.5
DEFAULT_FASTAPI_HOST = "127.0.0.1"

# After the initial ``/start\r`` injection, claude's TUI occasionally shows
# the typed ``/start`` but never registers the trailing CR — the submit gets
# swallowed during a startup re-render, so the task never begins. To make
# auto-start robust we follow up with a few bare CR "nudges": each one
# re-triggers submission of the already-typed ``/start``. If the first CR did
# register, the input box is empty and a bare CR is a harmless no-op.
DEFAULT_AUTO_START_NUDGE_DELAY = 0.5
AUTO_START_NUDGE_COUNT = 3

# Wait for Claude's input prompt to actually render before injecting ``/start``.
# Claude Code v2.1.119 changed Enter routing so a CR fires the topmost
# modal/dialog (permission prompt, theme picker, IDE detect, etc.) rather than
# also submitting input-box text; if any such dialog is open when our injection
# lands, the ``\r`` is consumed by the dialog and ``/start`` stays typed but
# unsubmitted. Waiting for the input-prompt glyph (``❯``, U+276F) gives Claude
# time to finish its startup chain before we send Enter.
DEFAULT_AUTO_START_READY_MARKER = b"\xe2\x9d\xaf"  # "❯" — Claude's prompt char
DEFAULT_AUTO_START_READY_TIMEOUT = 15.0

# Text typed to begin the task once the TUI is ready. Claude Code's native
# ``/start`` slash command is the historical default; other harnesses get
# their profile's start command (see lmer_cli.harness.SupervisorProfile),
# overridable via ``LMER_START_COMMAND``.
DEFAULT_START_COMMAND = "/start"

# Payloads written (with the shutdown chord gap between them) to make the
# wrapped TUI exit cleanly — claude's quit chord, Ctrl-C twice. Other
# harnesses get their profile's sequence, overridable via
# ``LMER_QUIT_SEQUENCE`` (steps separated by ``|``, unicode-escape decoded).
DEFAULT_QUIT_SEQUENCE = (b"\x03", b"\x03")
# Small extra pause after the marker is observed: the prompt often renders
# during a multi-screen-redraw sequence, and a short settle helps the input
# box reach its steady, focused state before we type into it.
DEFAULT_AUTO_START_SETTLE_DELAY = 0.25

# Gap between the auto-``/start`` submission and the follow-up prompt
# (``LMER_START_PROMPT``) injection. On slow systems ``/start`` has not yet
# been recognized as a slash command by the time the prompt text arrives, so
# the prompt lands on the same input line (``/start <prompt text>``) instead of
# as the next conversation turn. A generous default gives the slash command
# time to register before we type; fast systems simply wait this once at
# startup. Tunable via ``--start-prompt-delay`` / ``LMER_START_PROMPT_DELAY``.
DEFAULT_START_PROMPT_DELAY = 2.0
OUTPUT_BUFFER_LIMIT = 1024 * 1024  # 1 MiB rolling buffer of child output

# When the host terminal (especially VSCode's integrated terminal) hasn't
# fully propagated its real size by the moment the container TTY is
# allocated, claude's TUI lays out for a stale 80x24-ish default and the
# screen looks jumbled until the user resizes. Re-query the host TTY a
# short delay after launch and re-apply to the master PTY so claude
# receives a SIGWINCH and re-renders with the correct dimensions.
DEFAULT_WINSIZE_RECHECK_DELAY = 0.5

# Bounds on the geometry the FastAPI ``/resize`` route will apply. Zero rows or
# columns is not "size unknown", it is a wedged terminal: a TUI lays out against
# whatever the PTY reports and then draws nothing, and a browser session has no
# host TTY whose SIGWINCH would put it back to a usable size — so 0 is refused
# rather than applied. The ceiling is equally deliberate:
# ``struct.pack("HHHH", ...)`` in :func:`_set_winsize` raises outright past
# 65535, and four-digit geometry is a client bug better answered with a 422 than
# handed to an ioctl.
MIN_WINSIZE_DIMENSION = 1
MAX_WINSIZE_DIMENSION = 1000

# Self-shutdown: the wrapped agent can ask the supervisor to quit the session
# (e.g. an in-container ``lmer-slack end-session`` so a Slack chat session frees
# its orchestrator slot instead of lingering until the idle timeout) by sending
# the supervisor process ``SIGUSR1``. The supervisor injects claude's quit chord
# — Ctrl-C twice, the same chord the host-side session reaper uses — and, if that
# does not make claude exit within the grace period, escalates to SIGTERM then
# SIGKILL on the child so the session always ends. The supervisor's own PID is
# published to the wrapped process (and its subprocesses) via the
# ``LMER_SUPERVISOR_PID`` environment variable so the in-container CLI knows whom
# to signal.
SUPERVISOR_PID_ENV = "LMER_SUPERVISOR_PID"
# Gap between the two Ctrl-C presses of the quit chord (mirrors the host-side
# reaper's terminate() pacing in slack_chat.sessions).
DEFAULT_SHUTDOWN_CHORD_GAP = 0.5
# How long to wait for claude to unwind after each shutdown step before
# escalating (quit chord -> SIGTERM -> SIGKILL).
DEFAULT_SHUTDOWN_ESCALATE_GRACE = 10.0
# Poll cadence while waiting for the child to exit between escalation steps.
SHUTDOWN_POLL_INTERVAL = 0.2

# Where the session's own log goes, and the one rule about these two names: they
# are a cross-version interface, so they may not change. The orchestrator
# platform bind-mounts a per-session host directory at CONTAINER_SESSION_LOG_DIR
# and then *probes* for the file inside it — a session started from an image
# whose lmer predates this feature simply leaves the directory empty, which is
# how the platform tells the two apart without asking anything about versions
# (lmer_platform.spawn.container_log_path_for). Rename either half and a running
# fleet quietly stops recording, so the value is spelled here once and imported
# by the mount side rather than repeated.
#
# The directory is never created here. Its existence *is* the request: a plain
# `lmer` run on a laptop has nothing mounted there and must keep writing nothing,
# and creating it would fill a container's writable layer with a log no reader
# will ever come looking for.
CONTAINER_SESSION_LOG_DIR = "/home/developer/.lmer-session"
SESSION_LOG_NAME = "session.log"

# 0600 rather than the umask's guess: this file is every byte the session drew,
# credentials the agent typed included, and the directory it lands in is shared
# with the host by a bind mount (the same reasoning as the transcript's
# lmer_platform.transcripts.TRANSCRIPT_FILE_MODE).
SESSION_LOG_MODE = 0o600


class OutputBuffer:
    """Thread-safe rolling buffer keyed by cumulative byte offset.

    Output produced by the wrapped process is appended via :meth:`append`.
    HTTP clients read via :meth:`read_since` using a monotonically
    increasing cursor (the cumulative byte count). Older bytes are evicted
    once the buffer exceeds ``limit``.

    It also keeps *when* the last chunk arrived (:attr:`idle_seconds`), and this
    is the right object to keep it on for one structural reason: :meth:`append`
    is the single funnel every byte the wrapped process produced passes through,
    and :meth:`read_since` — the ``/output`` route, and therefore the platform's
    re-attach drain — cannot reach it. So "how long has the harness been quiet"
    can only be moved by the harness *doing something*, never by somebody asking.

    Which settles what counts as activity: **output, and never input**. The
    question the fact answers is whether the harness is doing anything, and typed
    input that produces nothing back is exactly the idle case — an operator who
    answered a prompt and got no response has a wedged session, not a busy one, and
    a clock that counted their keystroke would report the opposite. Input reaches
    the child through :func:`run_supervisor`'s write path, which never touches this
    object. A keystroke the TUI *echoes* does move the clock, and rightly so: in
    raw mode that echo is the harness drawing.

    ``clock`` is :func:`time.monotonic` rather than the wall clock, and not for
    tests (though it is injectable for them): an NTP correction mid-session must
    not be able to report a negative idle, or a quiet session as busy. The wall
    clock is applied once, at read time, to say *when* that was
    (:func:`_activity_report`).
    """

    def __init__(
        self,
        limit: int = OUTPUT_BUFFER_LIMIT,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._start_offset = 0  # offset of first byte still in the buffer
        self._end_offset = 0    # offset just past the last byte ever written
        self._clock = clock
        # ``None`` until the first chunk, and it stays that way rather than being
        # seeded at construction: a session whose harness has not drawn a byte yet
        # has no last-output moment, and inventing one would date an event that
        # never happened. See :meth:`idle_seconds`.
        self._last_append: Optional[float] = None

    @property
    def end_offset(self) -> int:
        with self._lock:
            return self._end_offset

    @property
    def start_offset(self) -> int:
        with self._lock:
            return self._start_offset

    @property
    def idle_seconds(self) -> Optional[float]:
        """Seconds since the wrapped process last produced output, or ``None``.

        ``None`` means "nothing has been produced at all", which is a real answer
        and not an error: it covers a session's first moments, and it is the
        answer a reader has to be able to render as *nothing* rather than as
        zero — an idle of ``0.0`` says the harness just wrote something.

        Clamped at zero because a monotonic clock read on another thread can land
        a hair behind the one that recorded the append, and a negative idle is not
        a fact anybody can act on.
        """
        with self._lock:
            if self._last_append is None:
                return None
            return max(0.0, self._clock() - self._last_append)

    def append(self, data: bytes) -> None:
        if not data:
            return
        with self._cond:
            # Under the same lock as the bytes, so a reader can never see output
            # that the idle clock has not accounted for yet.
            self._last_append = self._clock()
            self._chunks.append(data)
            self._size += len(data)
            self._end_offset += len(data)
            while self._size > self._limit and self._chunks:
                head = self._chunks[0]
                if self._size - len(head) >= self._limit:
                    self._chunks.popleft()
                    self._size -= len(head)
                    self._start_offset += len(head)
                else:
                    drop = self._size - self._limit
                    self._chunks[0] = head[drop:]
                    self._size -= drop
                    self._start_offset += drop
                    break
            self._cond.notify_all()

    def read_since(self, cursor: int, timeout: float = 0.0) -> tuple[bytes, int, int]:
        """Return ``(data, next_cursor, dropped_bytes)`` for output past
        ``cursor``.

        If ``cursor`` is older than what the buffer still holds, the gap size
        is reported as ``dropped_bytes`` and the data starts from the
        oldest available offset.

        ``timeout`` blocks up to that many seconds waiting for new data when
        the buffer has nothing past ``cursor``.
        """
        deadline = time.monotonic() + timeout if timeout > 0 else None
        with self._cond:
            while self._end_offset <= cursor:
                if deadline is None:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=remaining)
            dropped = 0
            effective_cursor = cursor
            if effective_cursor < self._start_offset:
                dropped = self._start_offset - effective_cursor
                effective_cursor = self._start_offset
            if self._end_offset <= effective_cursor:
                return b"", self._end_offset, dropped
            offset = self._start_offset
            collected: list[bytes] = []
            for chunk in self._chunks:
                next_offset = offset + len(chunk)
                if next_offset <= effective_cursor:
                    offset = next_offset
                    continue
                start_in_chunk = max(0, effective_cursor - offset)
                collected.append(chunk[start_in_chunk:])
                offset = next_offset
            return b"".join(collected), self._end_offset, dropped


class SessionLog:
    """The session's own copy of its PTY output, written from inside the container.

    Why this exists at all, given that the host side already tees the same bytes
    into a file: the host tee lives in whatever process attached to the container
    — for an orchestrated session, a thread in the platform daemon holding the PTY
    *master*, which is an fd and not a path. That process dying takes the tee with
    it and no successor can re-open it, so the scrollback stops growing while the
    session inside the container carries on working (the failure
    :mod:`lmer_platform.reattach` was written to paper over). This log has no such
    dependency: the writer is the supervisor itself, so the record survives
    everything short of the container.

    Three properties are load-bearing, and each is a promise the read side relies
    on:

    - **The file itself is the signal.** The platform probes it rather than asking
      anything about versions (``lmer_platform.session_io.canonical_log``), so it
      is opened once at startup — before the wrapped command is even forked — and
      what is in it means "this session's log of record is here". Which is also
      why :meth:`write` *removes* it when it can no longer keep that promise: a
      frozen file that still claimed to be the record would strand a reader on a
      truncated log while the host tee beside it held everything.
    - **Unbuffered appends.** The reader is another process tailing the path while
      the session runs; anything held in this process's buffers is scrollback the
      operator does not have yet. ``O_APPEND`` for the same reason
      :func:`lmer_platform.reattach._append` uses it — the position is correct
      regardless of who else has the file open.
    - **Never fatal.** A session must not die, or block, because its log did.
      Every failure degrades to "no in-container log", which is exactly the state
      an older image is in, and which the platform already handles.
    """

    def __init__(self, path: str, fd: int) -> None:
        self.path = path
        self._fd: Optional[int] = fd
        self._lock = threading.Lock()

    @classmethod
    def open_if_mounted(
        cls, directory: Optional[str] = None, name: str = SESSION_LOG_NAME
    ) -> "Optional[SessionLog]":
        """Open the session log if a directory was mounted for it. Else ``None``.

        *directory* defaults to :data:`CONTAINER_SESSION_LOG_DIR` read at call
        time, so the constant stays the single spelling of the mount point.

        Appends rather than truncates: a truncating open would make a second
        supervisor in one container erase the first one's record, and there is no
        case where losing bytes already written is the better answer.
        """
        if directory is None:
            directory = CONTAINER_SESSION_LOG_DIR
        if not os.path.isdir(directory):
            return None
        path = os.path.join(directory, name)
        try:
            fd = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                SESSION_LOG_MODE,
            )
        except OSError as exc:
            # Warned rather than raised: the session is the point, and a platform
            # that finds no file falls back to the host-side tee.
            sys.stderr.write(
                f"lmer-supervisor: cannot open the session log {path!r}: {exc}; "
                f"this session's output is recorded only by whatever is attached "
                f"to the container\n"
            )
            return None
        return cls(path, fd)

    def write(self, chunk: bytes) -> None:
        """Append *chunk*. A failure abandons the log instead of freezing it.

        See the class docstring: the file's presence is a claim about what it
        contains, so a writer that can no longer append withdraws the claim by
        unlinking. The alternative — leaving a file that stops at the failure —
        would have the platform serve a truncated log as canonical and ignore the
        complete host-side copy beside it.
        """
        with self._lock:
            if self._fd is None:
                return
            try:
                # Looped because ``os.write`` is allowed to write less than it was
                # given, and a short write here is not a slow log but a hole in the
                # record — silent, and impossible to spot afterwards.
                view = memoryview(chunk)
                while view:
                    view = view[os.write(self._fd, view):]
            except OSError as exc:
                sys.stderr.write(
                    f"lmer-supervisor: cannot write the session log "
                    f"{self.path!r}: {exc}; removing it so the host-side log "
                    f"stays this session's record\n"
                )
                self._abandon()

    def close(self) -> None:
        """Release the fd. Idempotent, and safe to call on an abandoned log."""
        with self._lock:
            fd, self._fd = self._fd, None
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)

    def _abandon(self) -> None:
        """Unlink and stop writing. Caller holds the lock."""
        fd, self._fd = self._fd, None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(self.path)


class _InputBody(BaseModel):
    data: str
    append_newline: bool = False


class _OutputResponse(BaseModel):
    data: str
    cursor: int
    dropped_bytes: int


class _ResizeBody(BaseModel):
    """Geometry for ``POST /resize``, bounds-checked before it reaches the PTY.

    The bounds ride on the fields rather than living in the route body so
    FastAPI refuses a bad request before any ioctl runs and its 422 names the
    offending field in ``detail[].loc`` — the browser client that sent a stray
    ``0`` learns *which* dimension it got wrong instead of watching its
    terminal wedge.
    """

    rows: int = Field(ge=MIN_WINSIZE_DIMENSION, le=MAX_WINSIZE_DIMENSION)
    cols: int = Field(ge=MIN_WINSIZE_DIMENSION, le=MAX_WINSIZE_DIMENSION)


def _set_winsize(fd: int, rows: int, cols: int, *, strict: bool = False) -> None:
    """Set the window size on a TTY file descriptor.

    ``strict`` decides who hears about a failed ioctl, and the two modes exist
    because the callers differ in whether anyone is owed an answer:

    - The forgiving default serves the host-TTY path — the ``SIGWINCH`` handler
      and the post-launch recheck timer. Those fire on their own schedule and
      routinely race a PTY that is being torn down as the child exits; a
      terminal that is already gone is not something they can act on, so they
      shrug and let the session finish shutting down.
    - ``strict=True`` serves a caller that was *asked* to resize and has to
      reply — the FastAPI ``/resize`` route. Swallowing the error there would
      answer 200 with geometry that never reached the PTY, leaving a browser
      client rendering at the wrong size with no signal that its request was
      lost. Raising lets the route report the failure honestly.
    """
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        if strict:
            raise


def _activity_report(
    idle_seconds: Optional[float], *, now: Optional[datetime] = None
) -> dict:
    """The two ``/healthz`` fields that say when the harness last did something.

    Both spellings come out of the *same* reading of the idle clock, so they can
    never disagree with each other, and both are ``None`` together when nothing
    has been produced yet (:attr:`OutputBuffer.idle_seconds`) — reported as nulls
    rather than omitted keys, for the reason the geometry is (see
    :func:`_build_fastapi_app`).

    Why both: ``idle_seconds`` is the *measurement*, taken from one monotonic
    clock inside the container, and it is what a reader can act on without owning
    a correct clock of its own — a browser on a phone comparing a timestamp
    against its own idea of now is how a busy session comes to look abandoned.
    ``last_output_at`` is that same measurement placed on the wall clock, because
    it is the form that survives being written down: a number of seconds is only
    true at the instant it was read, and anything that logs or forwards this needs
    a moment, not an age.

    The wall clock is applied here, at read time, and only here. Recording it at
    append time instead would cost a second syscall per chunk and would freeze a
    pre-NTP-correction timestamp into the record.

    Rounded to a tenth: the question is "has this been quiet for a while", asked
    in minutes, and the remaining digits of a float are noise in a payload an
    operator reads.
    """
    if idle_seconds is None:
        return {"last_output_at": None, "idle_seconds": None}
    wall = datetime.now(timezone.utc) if now is None else now
    last = wall - timedelta(seconds=idle_seconds)
    return {
        "last_output_at": last.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "idle_seconds": round(idle_seconds, 1),
    }


def _preconfigure_pty_for_injection(fd: int) -> None:
    """Pre-clear line-discipline flags so injected ``/start\\r`` survives intact.

    A freshly-allocated PTY starts in cooked mode (``ICRNL``, ``ECHO``,
    ``ICANON`` all on). Until the child (claude) calls ``tcsetattr`` to switch
    to raw mode, anything we write to the master is processed by the kernel:

    - ``ICRNL`` translates the trailing CR of ``/start\\r`` into LF — claude
      then reads ``/start\\n`` in raw mode, where ``\\n`` is a literal newline
      in the input box, not Enter. The slash command sits typed-but-unsubmitted.
    - ``ECHO`` echoes the injection back through the master, where our
      forwarding loop renders it onto the host TTY as a visible blank line
      pushing the claude banner upward (the "Enters outside the input" the
      issue reporter saw).

    Clearing these flags pre-fork narrows that race so the injection lands as
    raw CR with no host-side echo. Claude is free to call ``tcsetattr`` itself
    afterward — its own raw-mode setup overrides ours and life goes on. The
    helper clears the flags unconditionally — there is no per-flag gating
    inside — but the call site skips it under ``--manual-start`` since
    nothing is injected then.
    """
    with contextlib.suppress(termios.error, OSError):
        attrs = termios.tcgetattr(fd)
        attrs[0] &= ~(termios.ICRNL | termios.INLCR | termios.IGNCR)
        attrs[3] &= ~(
            termios.ECHO
            | termios.ECHOE
            | termios.ECHOK
            | termios.ECHONL
            | termios.ICANON
        )
        termios.tcsetattr(fd, termios.TCSANOW, attrs)


def _get_winsize(fd: int) -> Optional[tuple[int, int]]:
    """Return ``(rows, cols)`` for ``fd`` or ``None`` if unavailable."""
    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)
    except OSError:
        return None
    rows, cols, _, _ = struct.unpack("HHHH", packed)
    return rows, cols


def _pick_ports(port_range: tuple[int, int], host: str, count: int) -> list[int]:
    """Pick ``count`` distinct free ports from the inclusive range.

    Ports are probed in random order; each candidate is bound (then released)
    to confirm it is currently free. Returns the chosen ports in the order they
    were found.

    The probe binds the *same* ``host`` the FastAPI service will use so the
    free-port check reflects the interface the service actually listens on.
    ``host`` defaults to loopback (``DEFAULT_FASTAPI_HOST``); binding to all
    interfaces (``0.0.0.0`` / ``::`` / ``""``) only happens when explicitly
    configured via ``LMER_FASTAPI_HOST`` / ``--fastapi-host`` to expose the
    control endpoint beyond the container — an intentional opt-in, not the
    default. (CodeQL's ``py/bind-socket-all-network-interfaces`` alert is
    expected for that opt-in path and is accepted by design.)

    Raises :class:`ValueError` for an inverted range or non-positive ``count``,
    and :class:`RuntimeError` if fewer than ``count`` free ports are available.
    """
    import random
    import socket

    low, high = port_range
    if low > high:
        raise ValueError(f"invalid port range {low}-{high}")
    if count <= 0:
        raise ValueError(f"count must be positive, got {count}")
    candidates = list(range(low, high + 1))
    random.shuffle(candidates)
    chosen: list[int] = []
    for port in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
            except OSError:
                continue
            chosen.append(port)
            if len(chosen) == count:
                return chosen
    raise RuntimeError(
        f"could not allocate {count} free port(s) in range {low}-{high} on {host} "
        f"(found {len(chosen)})"
    )


def _pick_port(port_range: tuple[int, int], host: str) -> int:
    """Try ports in random order from the inclusive range until one binds.

    Raises :class:`RuntimeError` if no port in the range is free.
    """
    try:
        return _pick_ports(port_range, host, 1)[0]
    except RuntimeError:
        low, high = port_range
        raise RuntimeError(f"no free port in range {low}-{high} on {host}")


def _parse_port_range(spec: str) -> tuple[int, int]:
    """Parse ``"LOW-HIGH"`` into a tuple of ints, raising ``ValueError``."""
    parts = spec.split("-")
    if len(parts) != 2:
        raise ValueError(f"port range must be 'LOW-HIGH', got {spec!r}")
    low = int(parts[0])
    high = int(parts[1])
    if low <= 0 or high <= 0 or low > high:
        raise ValueError(f"invalid port range {spec!r}")
    return low, high


def _resolve_fastapi_port(options: dict, env: Mapping[str, str]) -> int:
    """Pick the FastAPI port the supervisor should bind.

    Honors ``LMER_FASTAPI_PORT`` if set to a valid integer (the host lmer CLI
    pre-picks a port and passes it through this var so it can publish exactly
    that port). Falls back to picking a free port from
    ``options["port_range"]`` on ``options["host"]``.
    """
    raw = (env.get("LMER_FASTAPI_PORT") or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return _pick_port(options["port_range"], options["host"])


def _build_fastapi_app(
    output: OutputBuffer,
    write_input: "callable[[bytes], int]",
    token: str,
    *,
    resize: Optional[Callable[[int, int], None]] = None,
    get_winsize: Optional[Callable[[], Optional[tuple[int, int]]]] = None,
):
    """Construct the FastAPI app exposing ``/input``, ``/output`` and ``/resize``.

    ``write_input`` is called with the bytes to deliver to the wrapped
    process's stdin. ``token`` gates every endpoint via the
    ``Authorization: Bearer <token>`` header.

    ``/resize`` is for sessions with no host TTY behind them: a browser-rendered
    terminal inherits whatever geometry the PTY was created with and has no
    ``SIGWINCH`` to correct it, so the client itself has to say how wide it is.
    When a host TTY *is* attached it stays authoritative — its next
    ``SIGWINCH`` re-applies the host geometry over anything posted here.

    ``resize`` and ``get_winsize`` are how the routes reach the PTY master: the
    app only ever sees these callables, never the fd, so the control plane can
    be exercised without allocating a terminal. Both are optional because the
    app predates them and older callers pass three positional arguments — with
    neither supplied ``/input``/``/output`` behave exactly as before,
    ``/resize`` answers 503 (there is nothing to resize, which is a deployment
    fact, not a crash) and ``/healthz`` reports a null geometry.

    ``/healthz``'s ``rows``/``cols`` are reported for the client's benefit, not
    as an echo-back template: a PTY that nothing has sized yet answers with its
    real ``0x0`` (a browser session has no host TTY to seed it), which is
    precisely the client's cue that it must resize — while ``/resize`` refuses a
    zero dimension. So geometry read from ``/healthz`` is not automatically a
    valid ``/resize`` body; a client posts the size it wants, not the size it
    found.
    """
    from fastapi import FastAPI, Header, HTTPException, Query

    app = FastAPI(title="lmer claude supervisor", version="1")

    def _check_auth(authorization):
        expected = f"Bearer {token}"
        if not authorization or not secrets.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="invalid bearer token")

    @app.post("/input")
    def post_input(body: _InputBody, authorization: Optional[str] = Header(default=None)):
        _check_auth(authorization)
        payload = body.data
        # Append CR (\r), not LF (\n): claude's TUI runs in raw mode where
        # the Enter key arrives as \r. \n would be inserted as a literal
        # newline in the input box and never submit. The field is named
        # ``append_newline`` for backwards-compatible API shape but the
        # behavior is "press Enter after the text".
        if not body.append_newline:
            return {"bytes_written": write_input(payload.encode("utf-8"))}

        # Type it and submit it, ONCE. No follow-up "nudge" CRs on this path —
        # see :data:`_SUBMIT_UNCONFIRMED_NOTE` for the whole of why, in short:
        # a bare CR is a no-op only against an empty input box with no dialog on
        # screen, and this handler cannot see the screen. The auto-start path
        # nudges because it waits for an observed readiness marker first and runs
        # before the session can raise a permission prompt at all; this one runs
        # mid-session, which is exactly when one is up.
        written = write_input(_ensure_submit_cr(payload).encode("utf-8"))
        # Said in the reply rather than assumed away: the CR went to the PTY, and
        # whether the TUI registered it as a submit is not something this process
        # observes. A caller that wants certainty has the terminal view.
        return {
            "bytes_written": written,
            "submit_confirmed": False,
            "note": _SUBMIT_UNCONFIRMED_NOTE,
        }

    @app.get("/output", response_model=_OutputResponse)
    def get_output(
        cursor: int = Query(default=0, ge=0),
        timeout: float = Query(default=0.0, ge=0.0, le=30.0),
        authorization: Optional[str] = Header(default=None),
    ):
        _check_auth(authorization)
        data, next_cursor, dropped = output.read_since(cursor, timeout=timeout)
        return _OutputResponse(
            data=data.decode("utf-8", errors="replace"),
            cursor=next_cursor,
            dropped_bytes=dropped,
        )

    @app.post("/resize")
    def post_resize(body: _ResizeBody, authorization: Optional[str] = Header(default=None)):
        _check_auth(authorization)
        if resize is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "resize unavailable: this supervisor was started without "
                    "PTY resize support"
                ),
            )
        try:
            resize(body.rows, body.cols)
        except OSError as exc:
            # The PTY can go away under us (child exited, master closed). Report
            # it as an error the client can read instead of letting uvicorn turn
            # the ioctl failure into a bare 500 with a traceback in the log.
            raise HTTPException(
                status_code=500, detail=f"cannot set window size: {exc}"
            ) from exc
        return {"rows": body.rows, "cols": body.cols}

    @app.get("/healthz")
    def healthz(authorization: Optional[str] = Header(default=None)):
        _check_auth(authorization)
        # Geometry rides along on the liveness probe so a client learns what it
        # is attaching to in the same request that told it the endpoint is up,
        # and can skip a /resize that would only re-apply the current size.
        # Unknown geometry is reported as nulls rather than omitted keys: the
        # shape stays stable for clients that read it unconditionally, and a
        # failing ioctl must not turn a liveness probe into an error. An unsized
        # PTY's real 0x0 is passed through as-is — see this function's docstring
        # for why that is not a body to post straight back to /resize.
        size = None
        if get_winsize is not None:
            with contextlib.suppress(OSError):
                size = get_winsize()
        rows, cols = size if size is not None else (None, None)
        # Idleness rides along for the same reason the geometry does — a caller
        # learns what it is attached to in the request that proved it is up — and
        # because this is the only place the fact exists: the host cannot see the
        # difference between a harness that is working and one that finished and
        # is sitting at its prompt, since run state only moves when the session
        # *ends* (spec D24). "Nothing produced yet" answers null, exactly like an
        # unknown geometry, so a reader that renders it has one absent case to
        # handle rather than two.
        return {
            "ok": True,
            "cursor": output.end_offset,
            "rows": rows,
            "cols": cols,
            **_activity_report(output.idle_seconds),
        }

    return app


def _start_fastapi_server(
    app,
    host: str,
    port: int,
) -> "tuple[threading.Thread, callable[[], None]]":
    """Start uvicorn in a background thread.

    Returns the server thread and a callable to request shutdown.
    """
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    thread = threading.Thread(target=server.run, name="lmer-supervisor-fastapi", daemon=True)
    thread.start()

    def shutdown() -> None:
        server.should_exit = True

    return thread, shutdown


def _parse_quit_sequence(raw: str) -> tuple[bytes, ...]:
    """Parse an ``LMER_QUIT_SEQUENCE`` env value into quit-sequence steps.

    Steps are separated by ``|``; each step is unicode-escape decoded so
    control bytes can be spelled out (``\\x03|\\x03`` → two Ctrl-C presses,
    ``/quit\\r`` → typed command + Enter) — the shared
    :func:`lmer_cli.util.decode_escape_bytes` semantics, also used by the
    user-harness manifest fields. An empty value yields an empty sequence,
    which disables the chord step entirely (shutdown escalates straight to
    SIGTERM).
    """
    return tuple(
        decode_escape_bytes(part) for part in raw.split("|") if part
    )


def _decode_env_bytes(raw: str, fallback: bytes, what: str) -> bytes:
    """Decode a byte-valued env override, falling back on a malformed escape.

    ``decode_escape_bytes`` raises ``UnicodeDecodeError`` on an undecodable
    escape (a value ending in a lone backslash, say), and this runs during
    supervisor startup — so an unluckily-shaped value that used to be taken as
    literal bytes would kill the in-container supervisor outright (review on
    !154). Degrade the way the user-harness manifest path does: warn, keep the
    harness profile's default.
    """
    try:
        return decode_escape_bytes(raw)
    except UnicodeDecodeError as exc:
        sys.stderr.write(
            f"lmer-supervisor: cannot decode {what} {raw!r}: {exc}; "
            f"using the harness profile default\n"
        )
        return fallback


def _resolve_harness_profile():
    """Look up the active harness's supervisor profile from ``LMER_HARNESS``.

    Unknown names fall back to the claude profile with a warning rather than
    crashing — inside the container a broken env var must not take the whole
    session down, and claude's profile reproduces the historical behavior.
    """
    try:
        return resolve_harness().supervisor
    except UnknownHarnessError as exc:
        sys.stderr.write(f"lmer-supervisor: {exc}; using claude supervisor profile\n")
        return resolve_harness("claude").supervisor


def _resolve_options(args: argparse.Namespace) -> dict:
    """Combine CLI args with environment variables to produce options.

    CLI flags win over environment values, which win over the active
    harness's supervisor profile (``LMER_HARNESS``), which wins over the
    claude-shaped defaults. Boolean env vars accept ``1/true/yes``
    (case-insensitive).
    """
    def env_bool(name: str) -> bool:
        return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")

    profile = _resolve_harness_profile()

    fastapi_enabled = bool(args.fastapi) or env_bool("LMER_FASTAPI")
    manual_start = bool(args.manual_start) or env_bool("LMER_MANUAL_START")

    port_range_spec = args.fastapi_port_range or os.environ.get("LMER_FASTAPI_PORT_RANGE")
    port_range = _parse_port_range(port_range_spec) if port_range_spec else DEFAULT_PORT_RANGE

    host = args.fastapi_host or os.environ.get("LMER_FASTAPI_HOST") or DEFAULT_FASTAPI_HOST
    token = args.fastapi_token or os.environ.get("LMER_FASTAPI_TOKEN") or ""

    delay_raw = args.auto_start_delay
    if delay_raw is None:
        delay_raw = os.environ.get("LMER_AUTO_START_DELAY")
    auto_start_delay = float(delay_raw) if delay_raw is not None else DEFAULT_AUTO_START_DELAY

    nudge_raw = args.auto_start_nudge_delay
    if nudge_raw is None:
        nudge_raw = os.environ.get("LMER_AUTO_START_NUDGE_DELAY")
    auto_start_nudge_delay = (
        float(nudge_raw) if nudge_raw is not None else DEFAULT_AUTO_START_NUDGE_DELAY
    )

    ready_timeout_raw = args.auto_start_ready_timeout
    if ready_timeout_raw is None:
        ready_timeout_raw = os.environ.get("LMER_AUTO_START_READY_TIMEOUT")
    if ready_timeout_raw is not None:
        auto_start_ready_timeout = float(ready_timeout_raw)
    elif profile.ready_timeout is not None:
        auto_start_ready_timeout = profile.ready_timeout
    else:
        auto_start_ready_timeout = DEFAULT_AUTO_START_READY_TIMEOUT

    settle_raw = os.environ.get("LMER_AUTO_START_SETTLE_DELAY")
    auto_start_settle_delay = (
        float(settle_raw) if settle_raw is not None else DEFAULT_AUTO_START_SETTLE_DELAY
    )

    # Marker bytes come from env so a future TUI change can be patched
    # without a release, decoded with the shared escape semantics
    # (decode_escape_bytes — same encoding as LMER_QUIT_SEQUENCE and the
    # user-harness manifest fields; plain text like "❯" passes through
    # byte-for-byte, so pre-escape values keep working). Setting it to the
    # empty string disables marker gating (waits only on the initial +
    # timeout-bounded delays). Default comes from the harness's profile.
    marker_raw = os.environ.get("LMER_AUTO_START_READY_MARKER")
    auto_start_ready_marker = (
        _decode_env_bytes(
            marker_raw, profile.ready_marker, "LMER_AUTO_START_READY_MARKER"
        )
        if marker_raw is not None
        else profile.ready_marker
    )

    # Task start command typed once the TUI is ready; harness-profile default
    # (claude: the native /start slash command), patchable via env.
    start_command = os.environ.get("LMER_START_COMMAND")
    if start_command is None:
        start_command = profile.start_command

    # TUI quit sequence used for SIGUSR1 self-shutdown; harness-profile
    # default, patchable via env (see _parse_quit_sequence).
    quit_raw = os.environ.get("LMER_QUIT_SEQUENCE")
    quit_sequence = profile.quit_sequence
    if quit_raw is not None:
        try:
            quit_sequence = _parse_quit_sequence(quit_raw)
        except UnicodeDecodeError as exc:
            # Same hazard as the ready marker below: an undecodable escape must
            # degrade to the harness default, not take the supervisor down.
            sys.stderr.write(
                f"lmer-supervisor: cannot decode LMER_QUIT_SEQUENCE {quit_raw!r}: "
                f"{exc}; using the harness profile default\n"
            )

    recheck_raw = os.environ.get("LMER_WINSIZE_RECHECK_DELAY")
    winsize_recheck_delay = (
        float(recheck_raw) if recheck_raw is not None else DEFAULT_WINSIZE_RECHECK_DELAY
    )

    # Follow-up prompt injected right after the auto-/start (host CLI --prompt).
    # Env-only: the host CLI forwards it as LMER_START_PROMPT. Empty/unset means
    # no follow-up. Tied to auto-start, so it is a no-op under --manual-start.
    start_prompt = os.environ.get("LMER_START_PROMPT") or ""

    # Gap before the follow-up prompt is injected (see DEFAULT_START_PROMPT_DELAY).
    # CLI flag wins over the env var, which wins over the default.
    prompt_delay_raw = args.start_prompt_delay
    if prompt_delay_raw is None:
        prompt_delay_raw = os.environ.get("LMER_START_PROMPT_DELAY")
    start_prompt_delay = (
        float(prompt_delay_raw)
        if prompt_delay_raw is not None
        else DEFAULT_START_PROMPT_DELAY
    )

    return {
        "fastapi": fastapi_enabled,
        "manual_start": manual_start,
        "port_range": port_range,
        "host": host,
        "token": token,
        "auto_start_delay": auto_start_delay,
        "auto_start_nudge_delay": auto_start_nudge_delay,
        "auto_start_ready_marker": auto_start_ready_marker,
        "auto_start_ready_timeout": auto_start_ready_timeout,
        "auto_start_settle_delay": auto_start_settle_delay,
        "winsize_recheck_delay": winsize_recheck_delay,
        "start_command": start_command,
        "quit_sequence": quit_sequence,
        "start_prompt": start_prompt,
        "start_prompt_delay": start_prompt_delay,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lmer-supervisor",
        description="Wrap the Claude Code CLI under a PTY with optional FastAPI control.",
    )
    parser.add_argument("--fastapi", action="store_true", help="Enable FastAPI input/output endpoint")
    parser.add_argument("--manual-start", action="store_true", help="Do not auto-inject /start at startup")
    parser.add_argument("--fastapi-port-range", help="Port range LOW-HIGH (default 8700-8799)")
    parser.add_argument("--fastapi-host", help="Host to bind FastAPI (default 127.0.0.1)")
    parser.add_argument("--fastapi-token", help="Bearer token (auto-generated if not provided)")
    parser.add_argument("--auto-start-delay", type=float, help="Seconds before /start is injected (default 1.5)")
    parser.add_argument(
        "--auto-start-nudge-delay",
        type=float,
        help="Seconds between follow-up CR nudges that re-trigger /start submission (default 0.5)",
    )
    parser.add_argument(
        "--auto-start-ready-timeout",
        type=float,
        help=(
            "Max seconds to wait for claude's input prompt to render before "
            "injecting /start; 0 disables marker waiting (default 15)"
        ),
    )
    parser.add_argument(
        "--start-prompt-delay",
        type=float,
        help=(
            "Seconds to wait after the auto-/start submission before injecting "
            "the follow-up --prompt/LMER_START_PROMPT, so /start registers as a "
            "slash command first on slow systems (default 2.0)"
        ),
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command to run (typically: -- claude ...)")
    return parser


def _split_command(remainder: list[str]) -> list[str]:
    """Drop a leading ``--`` separator if present and return the command."""
    if remainder and remainder[0] == "--":
        remainder = remainder[1:]
    if not remainder:
        raise SystemExit(
            "lmer-supervisor: missing command. Usage: lmer-supervisor [options] -- claude [args...]"
        )
    return remainder


def _wait_for_ready_marker(
    output: OutputBuffer,
    marker: bytes,
    timeout: float,
    cancel: Optional[threading.Event] = None,
) -> bool:
    """Block until ``marker`` appears in ``output``, up to ``timeout`` seconds.

    Returns ``True`` if the marker was observed in the byte stream, ``False`` if
    the timeout expired or ``cancel`` fired before that. ``timeout <= 0`` or an
    empty ``marker`` skips the wait entirely (returns ``True``) so callers can
    disable marker-gating by configuration. A small tail of the previously-seen
    bytes is retained between reads so a marker straddling two reads is still
    detected.

    Per-call read timeout is bounded (200 ms) so the loop wakes regularly to
    re-check the cancel event — without this, a shutdown raised during the wait
    would only be observed after the full ``timeout`` elapsed (the parent
    blocks in ``OutputBuffer.read_since`` and the cancel signal would be
    ignored for that whole window).
    """
    if not marker or timeout <= 0:
        return True
    deadline = time.monotonic() + timeout
    cursor = 0
    tail = b""
    keep = len(marker) - 1
    poll = 0.2  # short enough to make cancel feel snappy, long enough not to spin
    while True:
        if cancel is not None and cancel.is_set():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        data, cursor, _ = output.read_since(cursor, timeout=min(remaining, poll))
        if not data:
            continue
        haystack = tail + data
        if marker in haystack:
            return True
        if keep > 0:
            tail = haystack[-keep:]


def _start_auto_start_thread(
    output: OutputBuffer,
    write: Callable[[bytes], int],
    options: dict,
    cancel: threading.Event,
) -> threading.Thread:
    """Spawn the auto-/start daemon thread and return it.

    Sequencing inside the thread:

    1. Initial fixed delay — covers the very-early-startup window where claude
       has not produced any output yet (so the marker scan would just spin) and
       where any typing would be discarded.
    2. Marker wait — block until claude has rendered its input prompt glyph
       (see :func:`_wait_for_ready_marker`). Falls through on timeout so a
       marker change in claude doesn't deadlock the auto-start path; the
       cooked-mode pre-clear + CR nudges still give best-effort delivery.
    3. Settle delay — small extra pause to let the input box reach steady
       state if the prompt rendered mid-redraw.
    4. Inject ``/start\\r`` plus follow-up CR nudges.
    5. If a follow-up prompt is configured (host CLI ``--prompt`` →
       ``LMER_START_PROMPT``), wait ``start_prompt_delay`` seconds
       (``--start-prompt-delay`` / ``LMER_START_PROMPT_DELAY``) so ``/start``
       has registered as a slash command — otherwise on a slow system the
       prompt text lands on the same input line as ``/start`` — then type and
       submit it. Claude queues input typed while it is still working on
       ``/start``, so the prompt lands as the next turn. Skipped when no prompt
       is set.

    The ``cancel`` event short-circuits each stage so a shutdown raised during
    the wait is honored promptly (the marker wait re-checks the event on a
    bounded poll cadence internally).
    """
    nudge_delay = max(0.0, options["auto_start_nudge_delay"])
    initial_delay = max(0.0, options["auto_start_delay"])
    ready_marker = options["auto_start_ready_marker"]
    ready_timeout = max(0.0, options["auto_start_ready_timeout"])
    settle_delay = max(0.0, options["auto_start_settle_delay"])
    start_command = options.get("start_command", DEFAULT_START_COMMAND)
    start_prompt = options["start_prompt"]
    start_prompt_delay = max(0.0, options["start_prompt_delay"])

    def _run() -> None:
        if initial_delay > 0 and cancel.wait(initial_delay):
            return
        _wait_for_ready_marker(output, ready_marker, ready_timeout, cancel=cancel)
        if cancel.is_set():
            return
        if settle_delay > 0 and cancel.wait(settle_delay):
            return
        _inject_auto_start(write, AUTO_START_NUDGE_COUNT, nudge_delay, start_command)
        if start_prompt:
            # Pause so the follow-up doesn't interleave with the trailing CR
            # nudges (which would submit it prematurely or into a non-empty box)
            # and, more importantly, so /start has time to register as a slash
            # command before the prompt text is typed — otherwise on a slow
            # system the prompt lands on the same input line as /start (#65).
            if start_prompt_delay > 0 and cancel.wait(start_prompt_delay):
                return
            _inject_start_prompt(
                write, start_prompt, AUTO_START_NUDGE_COUNT, nudge_delay
            )

    thread = threading.Thread(
        target=_run, name="lmer-supervisor-auto-start", daemon=True
    )
    thread.start()
    return thread


#: What "press Enter" is on the wire: CR, not LF. A raw-mode TUI reads ``\r`` as
#: Enter and inserts ``\n`` as a literal newline in the input box.
_SUBMIT_CR = b"\r"

#: What ``/input`` says about a submit it cannot see land.
#:
#: This path used to follow every submitted message with two bare-CR "nudges",
#: on the argument that "once the text has gone through, the prompt is empty and
#: a bare CR is a no-op there". That is true only of an empty input box *with no
#: dialog on screen*. Since Claude Code v2.1.119 a CR fires the topmost
#: modal/dialog — the same routing change this module's own auto-start path
#: exists to work around, which is why that path waits for
#: :data:`DEFAULT_AUTO_START_READY_MARKER` before it types anything.
#:
#: The two paths are not alike. Auto-start runs once, at startup, after an
#: *observed* readiness marker, before the session can have raised a permission
#: prompt. ``/input`` runs on every message an operator or uber lmer sends
#: through ``POST /api/sessions/{id}/input``, the chat pane, ``lmer pipe`` and
#: the slack path — mid-session, which is precisely when a tool-permission prompt
#: is on screen, because the agent raises one while it is working and the
#: operator is watching. A blind CR 150ms behind the operator's "no, stop" takes
#: that prompt's default, and nothing in the transcript says a CR did it.
#:
#: What the nudges remedied is real (a submit CR swallowed by a re-render leaves
#: the text typed and unsent), but remedying it here means predicting the screen
#: state at +150ms, which this process never reads. So the submit is sent once
#: and the uncertainty is reported instead of being resolved by guessing. The
#: session's terminal view is where an operator can both see and fix it.
_SUBMIT_UNCONFIRMED_NOTE = (
    "Enter was sent after the text. Whether the TUI registered it as a submit "
    "is not something the supervisor can observe, so if the message is still "
    "sitting in the input box, submit it from the session's terminal view."
)


def _ensure_submit_cr(payload: str) -> str:
    """Append a submit CR (``\\r``) unless ``payload`` already ends with one.

    CR (not LF) is "press Enter" in claude's raw-mode TUI; an already-present
    trailing CR is left intact so the caller never double-submits. Shared by the
    FastAPI ``/input`` handler and ``_inject_start_prompt`` so the submit
    convention lives in one place.

    A trailing **LF** is not a submit and no longer counts as one. In raw mode it
    is inserted as a literal newline in the input box, so ``"text\\n"`` with
    ``append_newline`` set used to be typed and never sent — the CR that
    eventually submitted it came from the follow-up nudges, which this path no
    longer has (:data:`_SUBMIT_UNCONFIRMED_NOTE`). The caller's own newline is
    kept, because it is what they asked for; the Enter is added behind it.
    """
    if payload.endswith("\r"):
        return payload
    return payload + "\r"


def _inject_auto_start(
    write: Callable[[bytes], int],
    nudge_count: int,
    nudge_delay: float,
    start_command: str = DEFAULT_START_COMMAND,
) -> None:
    """Type the start command and submit it, then send a few bare-CR nudges.

    ``start_command`` is claude's native ``/start`` slash command by default;
    other harnesses inject their profile's plain-text start instruction.

    CR (``\\r``), not LF (``\\n``): the TUIs run in raw mode where Enter
    arrives as ``\\r``; ``\\n`` would be inserted as a literal newline in the
    input box and never trigger submission.

    The initial CR is sometimes swallowed during a startup re-render, leaving
    the command typed but unsubmitted — the bug behind this nudge logic. Each
    follow-up bare CR re-submits the already-typed command; once it has gone
    through, the prompt is empty and a bare CR is a harmless no-op. ``OSError``
    is suppressed so a closed PTY (child already exited) doesn't crash the
    timer thread this runs on.
    """
    payload = _ensure_submit_cr(start_command).encode("utf-8")
    with contextlib.suppress(OSError):
        write(payload)
    for _ in range(nudge_count):
        if nudge_delay > 0:
            time.sleep(nudge_delay)
        with contextlib.suppress(OSError):
            write(_SUBMIT_CR)


def _inject_start_prompt(
    write: Callable[[bytes], int],
    prompt: str,
    nudge_count: int = 0,
    nudge_delay: float = 0.0,
) -> None:
    """Type a follow-up prompt and submit it after ``/start`` was injected.

    Sends the prompt text followed by a submit CR (``\\r``) — Enter in claude's
    raw-mode TUI (via :func:`_ensure_submit_cr`, so a trailing **CR** already
    present on ``prompt`` is not doubled; a trailing **LF** is not a submit and
    gets a CR behind it, so an ``LMER_START_PROMPT`` ending in a newline is now
    submitted where it previously was not). Then sends ``nudge_count`` bare-CR
    nudges, mirroring :func:`_inject_auto_start`: the prompt's submit CR can be
    swallowed during a startup re-render just like ``/start``'s, leaving the
    text typed-but-unsubmitted with nothing to re-trigger it. Each follow-up
    bare CR re-submits it; once it has gone through, the box is empty and a bare
    CR is a harmless no-op. An empty prompt is a no-op. ``OSError`` is suppressed
    so a closed PTY (child already exited) doesn't crash the daemon thread this
    runs on.

    The prompt is written as a single payload under the supervisor's write lock,
    so it never interleaves with concurrent writers (FastAPI ``/input``, the
    main forwarding loop). Claude queues input typed while it is still working
    on ``/start``, so the submitted prompt becomes the next conversation turn.
    """
    if not prompt:
        return
    payload = _ensure_submit_cr(prompt)
    with contextlib.suppress(OSError):
        write(payload.encode("utf-8"))
    for _ in range(nudge_count):
        if nudge_delay > 0:
            time.sleep(nudge_delay)
        with contextlib.suppress(OSError):
            write(_SUBMIT_CR)


def _inject_shutdown_chord(
    write: Callable[[bytes], int],
    gap: float,
    sequence: tuple[bytes, ...] = DEFAULT_QUIT_SEQUENCE,
) -> None:
    """Send the harness's quit sequence with a short gap between steps.

    The default is claude's quit chord — Ctrl-C (``\\x03``) twice — the same
    chord the host-side session reaper writes in
    ``slack_chat.sessions.SessionManager.terminate`` to unwind the whole
    claude/container stack gracefully. The gap gives the TUI time to react to
    each step (e.g. render claude's "Press Ctrl-C again to exit" state so the
    second press is interpreted as the confirmation rather than coalesced into
    one). ``OSError`` is suppressed so a PTY that has already closed (child
    exited under us) doesn't crash the daemon thread this runs on. A
    non-positive ``gap`` never sleeps (so a negative value can't raise
    ``ValueError`` out of ``time.sleep``).
    """
    for i, step in enumerate(sequence):
        if i > 0 and gap > 0:
            time.sleep(gap)
        with contextlib.suppress(OSError):
            write(step)


def _child_alive(pid: int) -> bool:
    """Return whether ``pid`` is still a signalable process.

    Uses ``kill(pid, 0)`` (the POSIX existence probe) rather than ``waitpid`` so
    it never competes with ``run_supervisor``'s own reaping ``waitpid`` for the
    child — two reapers would race and one would get ``ECHILD``. A still-unreaped
    zombie reports alive here; that's fine, the escalation grace covers the brief
    window before the main loop reaps it.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but not signalable by us — treat as alive (we shouldn't see
        # this for our own child, but fail safe rather than escalate blindly).
        return True
    return True


def _wait_child_exit(pid: int, timeout: float) -> bool:
    """Poll until ``pid`` is gone or ``timeout`` seconds elapse.

    Returns ``True`` if the child exited within the window, ``False`` otherwise.
    """
    deadline = time.monotonic() + timeout
    while _child_alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(SHUTDOWN_POLL_INTERVAL, max(0.0, deadline - time.monotonic())))
    return True


def _self_shutdown(
    write: Callable[[bytes], int],
    child_pid: int,
    *,
    chord_gap: float = DEFAULT_SHUTDOWN_CHORD_GAP,
    escalate_grace: float = DEFAULT_SHUTDOWN_ESCALATE_GRACE,
    quit_sequence: tuple[bytes, ...] = DEFAULT_QUIT_SEQUENCE,
) -> None:
    """Quit the wrapped child on request, escalating until it actually exits.

    Ladder (mirrors the host-side reaper's ``terminate``):

    1. Inject the harness's quit sequence (claude: Ctrl-C twice). This lets
       the agent — and the docker/podman stack under it — unwind normally and
       exit 0.
    2. If the child is still alive after ``escalate_grace``, send it SIGTERM.
    3. If it is *still* alive after another ``escalate_grace``, send SIGKILL.

    Runs on a daemon thread spawned from the SIGUSR1 handler so the timed gaps
    don't execute in async-signal context. Signals are sent to the child PID
    only (not its group): the child is its own session/group leader (the fork
    path calls ``setsid``), and ``run_supervisor`` reaps it via ``waitpid``.
    """
    _inject_shutdown_chord(write, chord_gap, quit_sequence)
    if _wait_child_exit(child_pid, escalate_grace):
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(child_pid, signal.SIGTERM)
    if _wait_child_exit(child_pid, escalate_grace):
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(child_pid, signal.SIGKILL)


def run_supervisor(
    cmd: list[str],
    options: dict,
    *,
    stdin_fd: Optional[int] = None,
    stdout_fd: Optional[int] = None,
) -> int:
    """Run the supervisor loop.

    Spawns ``cmd`` under a PTY, forwards stdio, optionally serves the
    FastAPI endpoint, and optionally injects ``/start``. Returns the wrapped
    process exit code.

    ``stdin_fd`` / ``stdout_fd`` default to the real ``sys.stdin`` and
    ``sys.stdout`` file descriptors. They are parameterized for testing.
    """
    if stdin_fd is None:
        stdin_fd = sys.stdin.fileno()
    if stdout_fd is None:
        stdout_fd = sys.stdout.fileno()

    output = OutputBuffer()

    # Resolve FastAPI credentials and bind port BEFORE fork so the wrapped
    # process inherits LMER_FASTAPI_PORT / LMER_FASTAPI_TOKEN through its
    # initial environment. Doing this after fork would leave the child env
    # frozen and consumers inside the container would have no way to discover
    # an auto-generated token.
    fastapi_token = options["token"]
    fastapi_host = options["host"]
    fastapi_port: Optional[int] = None
    if options["fastapi"]:
        if not fastapi_token:
            fastapi_token = secrets.token_urlsafe(32)
        fastapi_port = _resolve_fastapi_port(options, os.environ)
        os.environ["LMER_FASTAPI_PORT"] = str(fastapi_port)
        os.environ["LMER_FASTAPI_TOKEN"] = fastapi_token

    # Publish the supervisor's own PID to the wrapped process (and everything it
    # spawns) BEFORE fork, so the child inherits it through its initial
    # environment. An in-container CLI (``lmer-slack end-session``) sends this PID
    # SIGUSR1 to ask for a graceful self-shutdown. Set after fork it would not
    # reach the already-frozen child env.
    os.environ[SUPERVISOR_PID_ENV] = str(os.getpid())

    # Before the fork, so the file exists from the earliest moment this session
    # could have produced a byte. A reader that finds it treats it as the log of
    # record (see SessionLog), and the window in which it is absent is a window in
    # which the reader is looking at the host-side tee instead — so the narrower
    # that window, the less of a session's start is described by two different
    # streams.
    session_log = SessionLog.open_if_mounted()

    master_fd, slave_fd = os.openpty()

    # Pre-clear ICRNL/ECHO/ICANON before fork so the auto-/start injection that
    # fires shortly after spawn isn't mangled by the PTY's default cooked-mode
    # line discipline. Skipped under --manual-start since nothing is injected.
    if not options["manual_start"]:
        _preconfigure_pty_for_injection(slave_fd)

    initial_winsize = _get_winsize(stdin_fd) if os.isatty(stdin_fd) else None
    if initial_winsize:
        _set_winsize(master_fd, *initial_winsize)

    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
            with contextlib.suppress(OSError):
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            if slave_fd > 2:
                os.close(slave_fd)
            os.close(master_fd)
            os.execvp(cmd[0], cmd)
        except Exception as exc:  # pragma: no cover - exec failure path
            os.write(2, f"lmer-supervisor: failed to exec {cmd[0]!r}: {exc}\n".encode())
            os._exit(127)

    os.close(slave_fd)

    # All writes to master_fd go through this lock so concurrent writers —
    # FastAPI's POST /input, the auto-/start timer, and the main forwarding
    # loop — never interleave bytes within a single payload.
    write_lock = threading.Lock()

    def write_to_child(data: bytes) -> int:
        with write_lock:
            return os.write(master_fd, data)

    # The FastAPI control plane touches the PTY's geometry through these two
    # closures instead of being handed the fd, so the app never has to know it
    # is talking to a terminal. No write lock: TIOCSWINSZ/TIOCGWINSZ don't move
    # bytes through the master, so they can't interleave with a payload write.
    # ``strict=True`` unlike the host-TTY callers: a client that posted /resize
    # is waiting on the result, so a failed ioctl has to reach the route and
    # become an error response instead of a 200 for geometry that never landed.
    def set_child_winsize(rows: int, cols: int) -> None:
        _set_winsize(master_fd, rows, cols, strict=True)

    def get_child_winsize() -> Optional[tuple[int, int]]:
        return _get_winsize(master_fd)

    fastapi_shutdown = None
    server_thread = None
    if options["fastapi"]:
        app = _build_fastapi_app(
            output,
            write_to_child,
            fastapi_token,
            resize=set_child_winsize,
            get_winsize=get_child_winsize,
        )
        server_thread, fastapi_shutdown = _start_fastapi_server(app, fastapi_host, fastapi_port)
        # Status line carries only host + port (no secret value): the bearer
        # token is never interpolated, only the name of the env var that holds
        # it. Host/port are read from plain locals above rather than the
        # secret-bearing options dict.
        sys.stderr.write(
            f"🛰  lmer-supervisor FastAPI listening on http://{fastapi_host}:{fastapi_port} "
            f"(bearer token in LMER_FASTAPI_TOKEN)\n"
        )
        sys.stderr.flush()

    auto_start_thread: Optional[threading.Thread] = None
    auto_start_cancel = threading.Event()
    if not options["manual_start"]:
        auto_start_thread = _start_auto_start_thread(
            output, write_to_child, options, auto_start_cancel
        )

    def _apply_host_winsize_to_master() -> None:
        size = _get_winsize(stdin_fd)
        if size:
            _set_winsize(master_fd, *size)

    winsize_recheck_timer: Optional[threading.Timer] = None
    if os.isatty(stdin_fd) and options["winsize_recheck_delay"] > 0:
        winsize_recheck_timer = threading.Timer(
            options["winsize_recheck_delay"], _apply_host_winsize_to_master
        )
        winsize_recheck_timer.daemon = True
        winsize_recheck_timer.start()

    old_attrs = None
    if os.isatty(stdin_fd):
        try:
            old_attrs = termios.tcgetattr(stdin_fd)
            tty.setraw(stdin_fd)
        except termios.error:
            old_attrs = None

    def forward_winsize(*_args) -> None:
        _apply_host_winsize_to_master()

    def forward_signal(sig: int, *_args) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, sig)

    # SIGUSR1 = "agent asked to leave": quit claude gracefully and report a clean
    # exit. The actual chord injection + escalation runs on a daemon thread so
    # the timed gaps never execute in async-signal context; the handler just
    # flips the flag (idempotent — repeated signals don't spawn racing threads)
    # and kicks the thread off.
    shutdown_requested = threading.Event()

    def request_shutdown(*_args) -> None:
        if shutdown_requested.is_set():
            return
        shutdown_requested.set()
        threading.Thread(
            target=_self_shutdown,
            args=(write_to_child, pid),
            kwargs={"quit_sequence": options.get("quit_sequence", DEFAULT_QUIT_SEQUENCE)},
            name="lmer-supervisor-self-shutdown",
            daemon=True,
        ).start()

    previous_winch = signal.signal(signal.SIGWINCH, forward_winsize) if hasattr(signal, "SIGWINCH") else None
    previous_int = signal.signal(signal.SIGINT, forward_signal)
    previous_term = signal.signal(signal.SIGTERM, forward_signal)
    previous_usr1 = (
        signal.signal(signal.SIGUSR1, request_shutdown)
        if hasattr(signal, "SIGUSR1")
        else None
    )

    stdin_open = True
    try:
        while True:
            watch = [master_fd]
            if stdin_open:
                watch.append(stdin_fd)
            try:
                rlist, _, _ = select.select(watch, [], [])
            except InterruptedError:
                continue
            except OSError as exc:
                if exc.errno == errno.EBADF:
                    break
                raise

            if master_fd in rlist:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                output.append(chunk)
                # Persisted before it is forwarded, deliberately: the file is the
                # copy that has to survive, stdout is the copy that might not, and
                # a record that lagged the stream derived from it would be the one
                # ordering a reader crossing from one to the other could not
                # tolerate (it would re-render bytes it already had).
                if session_log is not None:
                    session_log.write(chunk)
                with contextlib.suppress(OSError):
                    os.write(stdout_fd, chunk)

            if stdin_open and stdin_fd in rlist:
                try:
                    chunk = os.read(stdin_fd, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    # EOF on stdin: send EOT so a line-mode child can react,
                    # then stop forwarding stdin. We keep the PTY master open
                    # so the child isn't hit with SIGHUP, and continue
                    # streaming any remaining output until the child exits.
                    with contextlib.suppress(OSError):
                        write_to_child(b"\x04")
                    stdin_open = False
                    continue
                with contextlib.suppress(OSError):
                    write_to_child(chunk)
    finally:
        if auto_start_thread is not None:
            auto_start_cancel.set()
        if winsize_recheck_timer is not None:
            winsize_recheck_timer.cancel()
        if previous_winch is not None and hasattr(signal, "SIGWINCH"):
            signal.signal(signal.SIGWINCH, previous_winch)
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
        if previous_usr1 is not None and hasattr(signal, "SIGUSR1"):
            signal.signal(signal.SIGUSR1, previous_usr1)
        if old_attrs is not None:
            with contextlib.suppress(termios.error):
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
        if fastapi_shutdown is not None:
            fastapi_shutdown()
        if server_thread is not None:
            server_thread.join(timeout=2.0)
        # Only the fd goes; the file stays, because it is the record of a session
        # that has just ended and someone is about to read it back.
        if session_log is not None:
            session_log.close()

    _, status = os.waitpid(pid, 0)
    # A requested self-shutdown is a deliberate, clean sign-off: report exit 0 so
    # the orchestrator (e.g. the Slack session reaper) frees the slot quietly and
    # does NOT post a crash notice — even if the quit chord had to be escalated to
    # a signal, which would otherwise surface as a non-zero (128+sig) status.
    if shutdown_requested.is_set():
        return 0
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    cmd = _split_command(list(args.command))
    options = _resolve_options(args)
    return run_supervisor(cmd, options)


if __name__ == "__main__":
    raise SystemExit(main())
