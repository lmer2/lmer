"""
Claude Code supervisor process.

Wraps the Claude Code CLI under a PTY so a controlling process sits between
the user's terminal and claude. The supervisor:

- Allocates a PTY and spawns the wrapped command (typically ``claude``) with
  the slave end as stdin/stdout/stderr.
- Forwards bytes between the host TTY and the PTY master in both directions,
  preserving raw mode and propagating ``SIGWINCH`` for terminal resizes.
- Optionally exposes a FastAPI endpoint with two routes for programmatic
  read/write of the wrapped process. The endpoint is gated by ``--fastapi``
  (or ``LMER_FASTAPI=1``) and protected with a bearer token.
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
- Optionally injects a follow-up prompt (host CLI ``--prompt`` →
  ``LMER_START_PROMPT``) immediately after the ``/start`` injection so an
  automated run can hand claude an extra instruction without manual typing.
  Tied to auto-start, so it is a no-op under ``--manual-start``.

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
from typing import Callable, Iterable, Mapping, Optional

from pydantic import BaseModel


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
# Small extra pause after the marker is observed: the prompt often renders
# during a multi-screen-redraw sequence, and a short settle helps the input
# box reach its steady, focused state before we type into it.
DEFAULT_AUTO_START_SETTLE_DELAY = 0.25
OUTPUT_BUFFER_LIMIT = 1024 * 1024  # 1 MiB rolling buffer of child output

# When the host terminal (especially VSCode's integrated terminal) hasn't
# fully propagated its real size by the moment the container TTY is
# allocated, claude's TUI lays out for a stale 80x24-ish default and the
# screen looks jumbled until the user resizes. Re-query the host TTY a
# short delay after launch and re-apply to the master PTY so claude
# receives a SIGWINCH and re-renders with the correct dimensions.
DEFAULT_WINSIZE_RECHECK_DELAY = 0.5


class OutputBuffer:
    """Thread-safe rolling buffer keyed by cumulative byte offset.

    Output produced by the wrapped process is appended via :meth:`append`.
    HTTP clients read via :meth:`read_since` using a monotonically
    increasing cursor (the cumulative byte count). Older bytes are evicted
    once the buffer exceeds ``limit``.
    """

    def __init__(self, limit: int = OUTPUT_BUFFER_LIMIT) -> None:
        self._limit = limit
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._start_offset = 0  # offset of first byte still in the buffer
        self._end_offset = 0    # offset just past the last byte ever written

    @property
    def end_offset(self) -> int:
        with self._lock:
            return self._end_offset

    @property
    def start_offset(self) -> int:
        with self._lock:
            return self._start_offset

    def append(self, data: bytes) -> None:
        if not data:
            return
        with self._cond:
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


class _InputBody(BaseModel):
    data: str
    append_newline: bool = False


class _OutputResponse(BaseModel):
    data: str
    cursor: int
    dropped_bytes: int


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """Set the window size on a TTY file descriptor."""
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


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


def _pick_port(port_range: tuple[int, int], host: str) -> int:
    """Try ports in random order from the inclusive range until one binds.

    Raises :class:`RuntimeError` if no port in the range is free.
    """
    import random
    import socket

    low, high = port_range
    if low > high:
        raise ValueError(f"invalid port range {low}-{high}")
    candidates = list(range(low, high + 1))
    random.shuffle(candidates)
    for port in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
            except OSError:
                continue
            return port
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
):
    """Construct the FastAPI app exposing ``/input`` and ``/output``.

    ``write_input`` is called with the bytes to deliver to the wrapped
    process's stdin. ``token`` gates both endpoints via the
    ``Authorization: Bearer <token>`` header.
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
        if body.append_newline:
            payload = _ensure_submit_cr(payload)
        n = write_input(payload.encode("utf-8"))
        return {"bytes_written": n}

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

    @app.get("/healthz")
    def healthz(authorization: Optional[str] = Header(default=None)):
        _check_auth(authorization)
        return {"ok": True, "cursor": output.end_offset}

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


def _resolve_options(args: argparse.Namespace) -> dict:
    """Combine CLI args with environment variables to produce options.

    CLI flags win over environment values. Boolean env vars accept
    ``1/true/yes`` (case-insensitive).
    """
    def env_bool(name: str) -> bool:
        return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")

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
    auto_start_ready_timeout = (
        float(ready_timeout_raw)
        if ready_timeout_raw is not None
        else DEFAULT_AUTO_START_READY_TIMEOUT
    )

    settle_raw = os.environ.get("LMER_AUTO_START_SETTLE_DELAY")
    auto_start_settle_delay = (
        float(settle_raw) if settle_raw is not None else DEFAULT_AUTO_START_SETTLE_DELAY
    )

    # Marker bytes are read as UTF-8 from env so a future claude UI change can
    # be patched without a release. Setting it to the empty string disables
    # marker gating (waits only on the initial + timeout-bounded delays).
    marker_raw = os.environ.get("LMER_AUTO_START_READY_MARKER")
    auto_start_ready_marker = (
        marker_raw.encode("utf-8") if marker_raw is not None
        else DEFAULT_AUTO_START_READY_MARKER
    )

    recheck_raw = os.environ.get("LMER_WINSIZE_RECHECK_DELAY")
    winsize_recheck_delay = (
        float(recheck_raw) if recheck_raw is not None else DEFAULT_WINSIZE_RECHECK_DELAY
    )

    # Follow-up prompt injected right after the auto-/start (host CLI --prompt).
    # Env-only: the host CLI forwards it as LMER_START_PROMPT. Empty/unset means
    # no follow-up. Tied to auto-start, so it is a no-op under --manual-start.
    start_prompt = os.environ.get("LMER_START_PROMPT") or ""

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
        "start_prompt": start_prompt,
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
       ``LMER_START_PROMPT``), type and submit it after a short pause. Claude
       queues input typed while it is still working on ``/start``, so the
       prompt lands as the next turn. Skipped when no prompt is set.

    The ``cancel`` event short-circuits each stage so a shutdown raised during
    the wait is honored promptly (the marker wait re-checks the event on a
    bounded poll cadence internally).
    """
    nudge_delay = max(0.0, options["auto_start_nudge_delay"])
    initial_delay = max(0.0, options["auto_start_delay"])
    ready_marker = options["auto_start_ready_marker"]
    ready_timeout = max(0.0, options["auto_start_ready_timeout"])
    settle_delay = max(0.0, options["auto_start_settle_delay"])
    start_prompt = options["start_prompt"]

    def _run() -> None:
        if initial_delay > 0 and cancel.wait(initial_delay):
            return
        _wait_for_ready_marker(output, ready_marker, ready_timeout, cancel=cancel)
        if cancel.is_set():
            return
        if settle_delay > 0 and cancel.wait(settle_delay):
            return
        _inject_auto_start(write, AUTO_START_NUDGE_COUNT, nudge_delay)
        if start_prompt:
            # Pause so the follow-up doesn't interleave with the trailing CR
            # nudges (which would submit it prematurely or into a non-empty box).
            if nudge_delay > 0 and cancel.wait(nudge_delay):
                return
            _inject_start_prompt(
                write, start_prompt, AUTO_START_NUDGE_COUNT, nudge_delay
            )

    thread = threading.Thread(
        target=_run, name="lmer-supervisor-auto-start", daemon=True
    )
    thread.start()
    return thread


def _ensure_submit_cr(payload: str) -> str:
    """Append a submit CR (``\\r``) unless ``payload`` already ends with CR/LF.

    CR (not LF) is "press Enter" in claude's raw-mode TUI; an already-present
    trailing CR/LF is left intact so the caller never double-submits. Shared by
    the FastAPI ``/input`` handler and ``_inject_start_prompt`` so the submit
    convention lives in one place.
    """
    if payload.endswith(("\r", "\n")):
        return payload
    return payload + "\r"


def _inject_auto_start(
    write: Callable[[bytes], int],
    nudge_count: int,
    nudge_delay: float,
) -> None:
    """Type ``/start`` and submit it, then send a few bare-CR nudges.

    CR (``\\r``), not LF (``\\n``): claude's TUI runs in raw mode where Enter
    arrives as ``\\r``; ``\\n`` would be inserted as a literal newline in the
    input box and never trigger submission.

    The initial CR is sometimes swallowed during a startup re-render, leaving
    ``/start`` typed but unsubmitted — the bug behind this nudge logic. Each
    follow-up bare CR re-submits the already-typed ``/start``; once it has gone
    through, the prompt is empty and a bare CR is a harmless no-op. ``OSError``
    is suppressed so a closed PTY (child already exited) doesn't crash the
    timer thread this runs on.
    """
    with contextlib.suppress(OSError):
        write(b"/start\r")
    for _ in range(nudge_count):
        if nudge_delay > 0:
            time.sleep(nudge_delay)
        with contextlib.suppress(OSError):
            write(b"\r")


def _inject_start_prompt(
    write: Callable[[bytes], int],
    prompt: str,
    nudge_count: int = 0,
    nudge_delay: float = 0.0,
) -> None:
    """Type a follow-up prompt and submit it after ``/start`` was injected.

    Sends the prompt text followed by a submit CR (``\\r``) — Enter in claude's
    raw-mode TUI (via :func:`_ensure_submit_cr`, so a trailing CR/LF already
    present on ``prompt`` is not doubled). Then sends ``nudge_count`` bare-CR
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
            write(b"\r")


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
    fastapi_port: Optional[int] = None
    if options["fastapi"]:
        if not fastapi_token:
            fastapi_token = secrets.token_urlsafe(32)
        fastapi_port = _resolve_fastapi_port(options, os.environ)
        os.environ["LMER_FASTAPI_PORT"] = str(fastapi_port)
        os.environ["LMER_FASTAPI_TOKEN"] = fastapi_token

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

    fastapi_shutdown = None
    server_thread = None
    if options["fastapi"]:
        app = _build_fastapi_app(output, write_to_child, fastapi_token)
        server_thread, fastapi_shutdown = _start_fastapi_server(app, options["host"], fastapi_port)
        sys.stderr.write(
            f"🛰  lmer-supervisor FastAPI listening on http://{options['host']}:{fastapi_port} "
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

    previous_winch = signal.signal(signal.SIGWINCH, forward_winsize) if hasattr(signal, "SIGWINCH") else None
    previous_int = signal.signal(signal.SIGINT, forward_signal)
    previous_term = signal.signal(signal.SIGTERM, forward_signal)

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
        if old_attrs is not None:
            with contextlib.suppress(termios.error):
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
        if fastapi_shutdown is not None:
            fastapi_shutdown()
        if server_thread is not None:
            server_thread.join(timeout=2.0)

    _, status = os.waitpid(pid, 0)
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
