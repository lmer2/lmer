"""Reading and writing a *running* session (spec D16, slice M2 / T16).

Everything else in this package observes the fleet. This module is the first
piece that reaches into a session and changes it, which is the difference between
a dashboard and an orchestrator. Three facts about the data decide its shape.

**The log is raw terminal bytes, so the server never decodes it.** Scrollback is
served base64-encoded. Not for transport safety — JSON would carry text fine —
but because a chunk boundary lands wherever the byte offset says it does, and
that is regularly in the middle of a UTF-8 sequence or halfway through a CSI
escape. Decoding server-side would replace those with U+FFFD and hand the client
a corrupted stream it can never repair. So the client gets bytes and a byte
cursor, and the terminal emulator does what it exists to do.

**The log outlives the container**, which is the whole reason a crashed session's
history is readable at all (see :mod:`lmer_platform.spawn`). Nothing on the read
path therefore requires a live session, or even a registry entry: a session that
exited *cleanly* has had its entry removed while its log is still on disk, and
that scrollback must still be servable. "No entry" is a normal answer here, not
an error.

**Writing goes through the session's own control plane**, never the PTY log — the
log is a tee, not a terminal. The bearer token comes from
:func:`lmer_platform.spawn.read_control_token`, which derives the path from the
session id rather than dereferencing the ``token_ref`` on the entry. That is
deliberate: registry entries are hand-editable debugging artifacts, and following
a path out of one would let a tampered entry choose which file the daemon reads
and forwards to a session. The token is never returned, never logged, and kept
out of :class:`ControlEndpoint`'s ``repr`` — the first thing anyone does with a
dataclass in a bad moment is log it.

Two logs, one of them canonical
-------------------------------
A session can have its output on disk twice, written by two processes that never
see each other's file, and :func:`canonical_log` is the one place that chooses
between them:

1. **The session's own log** (:func:`lmer_platform.spawn.container_log_path_for`),
   written by the supervisor *inside* the container into a directory the platform
   mounted. Canonical from its first byte onward, because it is the only copy that
   does not depend on a host process staying alive: the alternative below is fed by
   a thread holding a PTY master, which is an fd and dies with the daemon (T36).
2. **The host-side PTY tee** (:func:`lmer_platform.spawn.log_path_for`), which is
   the record for every session that does not write the first one — and there will
   always be such sessions.

The choice is a **probe, never a version check**, and that is load-bearing rather
than stylistic: the supervisor that writes the first log ships in the *session
image*, not in the daemon's own install, so a fleet always contains workers that
will never write it. A daemon that inferred "this session must have an
in-container log" from its own version would serve an empty file for those. So:
that file holds bytes, it is the log; it does not, everything behaves exactly as
it did before the file existed, indefinitely.

**They are not merged, and the streams are not the same stream.** The host tee
holds what ``lmer`` printed on the host — image pull, clone, its own chatter —
followed by everything the container forwarded; the in-container log holds only
what the harness's PTY produced. Stitching them would mean guessing where one
ends inside the other, which is the mistake the control-plane read path
(``/output``, below) already documents at length. One source per read.

A reader may still ask for the *other* one by name (:func:`named_log`, and the
``source`` parameter on :func:`read_log` and the route above it), because "not
merged" left the host-side launch of a modern session unreachable: everything
printed before the container had a log to write into is in the tee, and paging
back through the canonical log stops at the harness's first byte. That read is
read-only, one named file at a time, and still in that file's own offset space —
it is a second view, not a second cursor.

Offsets, and the one seam this leaves
-------------------------------------
An offset is a byte position **in whichever log is canonical for that session**,
which is a single well-defined space for the whole life of a stream:
:func:`follow_log` resolves the source once and keeps it, so a socket never sees
the meaning of its cursor change underneath it.

It can change *between* streams, exactly once and only early: a session's first
moments have no in-container log (the container is still starting), so a client
that fetched its tail then and opened a socket a moment later hands back a cursor
measured in the other file. The resolution is deliberately the dumb one —
:func:`read_log_at` clamps a cursor past the end to the end — and it is chosen
because of *which way* it is wrong. Clamping can skip a bounded stretch of output
around the seam (at most the host-side preamble's worth, and the seam normally
falls where the in-container log is still empty); the alternative, rewinding to
serve from the start of the other file, would re-render bytes the terminal already
has. A skipped fragment of a redraw is repaired by the next one a TUI paints; a
duplicated one corrupts the emulator's screen until the client reloads.

Nothing here rewrites the bytes it serves — not the credential shapes
:func:`lmer_platform.transcripts._scrub` masks, not the configured secret it
strikes out by value. That asymmetry is deliberate and predates the second log
(``transcripts`` says so from its side: the log route "already serves those same
bytes", and what it scrubs is a *new* payload built for a browser to display).
Substituting a value in this stream would move every offset after it, so a
scrubbed log and the cursor discipline above cannot both hold; the log is
protected by the 0700 directory and the 0600 file instead, and the in-container
log is created with exactly those modes for that reason
(:data:`lmer_cli.supervisor.SESSION_LOG_MODE`).

Why the WebSocket needs a ticket instead of the shared secret
-------------------------------------------------------------
The REST routes are gated by the shared secret, and a browser supplies it as
Basic credentials automatically once prompted. A WebSocket handshake gets neither:
the ``WebSocket`` constructor accepts no headers, and the credentials the browser
holds for the origin are not applied to a ``ws://`` upgrade. The tempting fix,
``?secret=…``, writes the long-lived credential into exactly the places a
credential must never reach — the access log, the browser's history and devtools,
and any proxy in between — where it survives long after the socket closed.

So an authenticated ``POST`` mints a :class:`TicketStore` ticket and the socket
carries *that* in its query string: opaque, bound to one session id, redeemable
once, and expiring in :data:`TICKET_TTL_SECONDS`. A leaked ticket buys an attacker
one socket on one session for a few seconds; a leaked secret buys the whole
daemon forever. Tickets are the socket's *only* credential — a bearer header is
not also accepted — so there is one way in to reason about, and a CLI client pays
one extra authenticated request for it.

The store is in memory and per-process, which is sound because the daemon *is* one
process (spec §6.1 makes it the single writer of platform state). If it ever runs
behind multiple workers, tickets must move with it — a ticket minted by worker A
and presented to worker B would look expired, and the failure would present as an
intermittently broken terminal.

Reading over the control plane, and why that is a *second* read path
--------------------------------------------------------------------
The paragraph above ("writing goes through the control plane, never the PTY log")
describes an asymmetry that turned out to be a bug: writes survive a daemon
restart because they are an HTTP call to a process in the container, while reads
came only from the PTY log — a file fed by a drain thread that owns the PTY
master. The master is an fd, not a path, so it dies with the daemon and no
successor can re-open it; the log then stops growing while the session inside the
container carries on working. That is exactly what an operator saw: a terminal
that still accepted input and never printed another byte.

:func:`probe_health` and :func:`read_control_output` are the read half of the
path writes already use, and :mod:`lmer_platform.reattach` is what drives them.
Two properties of that stream have to be understood before touching either:

- **It is a different stream from the log, in a different offset space.** The
  host PTY log holds what ``lmer`` printed *on the host* (its own chatter plus
  the container's output, teed together). ``/output`` reports the harness's PTY
  *inside* the container, counted by a bounded ring buffer with its own
  ``start``/``end`` offsets. A cursor from one is meaningless in the other, and
  treating them as one offset space duplicates or loses output.
- **The supervisor decodes before it answers.** ``/output`` returns
  ``data.decode("utf-8", errors="replace")``, so a multi-byte sequence split
  across a read boundary comes back as U+FFFD — the corruption the log path goes
  out of its way to avoid by serving raw bytes and a byte cursor.
  :class:`ControlOutput` re-encodes what it is given, and cannot undo that; the
  loss is upstream, in the route's response model.

Failures, and which ones are allowed to be quiet
------------------------------------------------
Every exception here carries the HTTP status it deserves, so the routes need one
handler and a new failure mode gets an explicit code instead of falling through
to a 500 with a traceback.

The loud/quiet split follows :mod:`lmer_platform.store` and
:mod:`lmer_platform.registry`: **input is loud** (an operator's answer that
silently went nowhere is worse than an error — the session is still sitting there
blocked), while **resize is quiet** (a terminal rendering at the wrong width is
cosmetic; a socket that drops because the geometry could not be applied is not).

:func:`session_activity` is the quiet end of that split taken one step further,
and it is quiet on the *same* route :func:`probe_health` raises on. The difference
is entirely in who is asking: a re-attach probes to decide whether a session is
there at all, while the idle reading is one fact on one row of a fleet view that
is polled from a phone. So it answers ``None`` for every failure, including a
session whose image is too old to have the fields — which is what keeps a mixed
fleet reading exactly as it did before they existed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Optional

import requests

from . import registry
from .spawn import container_log_path_for, log_path_for, read_control_token
from .store import append_event

logger = logging.getLogger("lmer_platform.session_io")

__all__ = [
    "SessionIOError", "SessionNotFound", "ControlUnavailable", "ControlPlaneError",
    "LogChunk", "ResizeReport", "ControlEndpoint", "ControlOutput", "TicketStore",
    "DEFAULT_LOG_LIMIT", "MAX_LOG_LIMIT", "DEFAULT_TAIL_BYTES",
    "POLL_INTERVAL_SECONDS", "TICKET_TTL_SECONDS", "MAX_LIVE_TICKETS",
    "CONTROL_TIMEOUT_SECONDS", "LOG_SOURCE_CONTAINER", "LOG_SOURCE_HOST",
    "session_log_path", "container_log_path", "canonical_log", "named_log",
    "require_session", "session_is_live", "read_log",
    "read_log_at", "follow_log", "control_endpoint", "send_input", "apply_resize",
    "probe_health", "read_control_output", "session_activity",
    "session_output_state",
    "control_plane_answers", "ACTIVITY_TIMEOUT_SECONDS",
]

#: ``LogChunk.source``: the session wrote this log itself, from inside its
#: container. The log of record wherever it exists — see the module docstring.
LOG_SOURCE_CONTAINER = "container"

#: ``LogChunk.source``: the host-side PTY tee. Every session has one; it is what
#: a session whose image predates the in-container log is served from.
LOG_SOURCE_HOST = "host"

#: Bytes served by one read when the caller does not say. Base64 inflates the
#: response by a third, so this bounds the answer rather than the request.
DEFAULT_LOG_LIMIT = 64 * 1024

#: Ceiling on a single read, matching the supervisor's own 1 MiB output buffer —
#: a client that wants more of a long run pages for it.
MAX_LOG_LIMIT = 1024 * 1024

#: Scrollback a terminal gets when it attaches without naming an offset. A fresh
#: socket wants the last screenful of a possibly enormous log, not all of it.
DEFAULT_TAIL_BYTES = 64 * 1024

#: How long the follower waits before re-reading a log that had nothing new.
#: This is the floor on how late a keystroke's echo reaches the browser, so it is
#: set by what a person typing notices rather than by what is cheap: four syscalls
#: on a page-cached file, twenty times a second, per attached terminal.
POLL_INTERVAL_SECONDS = 0.05

#: The smallest geometry the daemon will pass to a PTY. Not the control plane's
#: 1..1000 contract restated — a sanity floor: values under these come from a
#: client measuring a mid-layout sliver, never from a terminal a person is
#: looking at, and one such write reflowed a live TUI to a single column
#: (:func:`apply_resize` has the story). The narrowest real screens fit ~40
#: columns; half that is comfortably below any deliberate size and comfortably
#: above every observed artifact (1-2 columns).
MIN_RESIZE_COLS = 20
MIN_RESIZE_ROWS = 5

#: How long a tty ticket stays redeemable. Seconds, because the only legitimate
#: gap between minting one and opening the socket is a round trip.
TICKET_TTL_SECONDS = 30.0

#: Ceiling on unredeemed tickets kept in memory. Minting is authenticated, so
#: this is not a defence against an attacker — it is what keeps a client that
#: mints and never connects from growing the daemon's heap forever.
MAX_LIVE_TICKETS = 128

#: Control-plane calls cross loopback to a process that answers from memory, so
#: a slow one is a wedged one; failing at five seconds beats pinning a threadpool
#: worker (or the event loop's thread pool) indefinitely.
CONTROL_TIMEOUT_SECONDS = 5.0

#: Budget for the idle read (:func:`session_activity`), and much shorter than
#: :data:`CONTROL_TIMEOUT_SECONDS` because of who is waiting: this one runs while
#: a fleet payload is being assembled — the browser's poll, ``lmer platform
#: status``, and every detection tick — where the *whole* view waits on it, once
#: per live session. Five seconds each would let a couple of wedged containers
#: hold the fleet view past the interval that asked for it, and what would be lost
#: by giving up early is one row's "idle 22m". A control plane that answers from
#: memory over loopback and has not managed it in a second is not busy, it is
#: wedged — which the row says by reporting nothing.
ACTIVITY_TIMEOUT_SECONDS = 1.0

#: Where a session's control plane lives when its entry does not say. Matches
#: ``spawn._CONTROL_HOST``; entries written by any lmer that had a control plane
#: carry the host explicitly, so this only covers a hand-written one.
_DEFAULT_CONTROL_HOST = "127.0.0.1"

#: Upstream error text is echoed to the client, so it is truncated rather than
#: relayed unbounded.
_DETAIL_LIMIT = 500


class SessionIOError(RuntimeError):
    """Base for every failure here, carrying the status a route should answer.

    The status rides on the exception so the routes can have a single handler:
    the alternative is a chain of ``except`` clauses that a new failure mode gets
    left out of, and "left out" means a 500 with a traceback in the log.
    """

    status = 500


class SessionNotFound(SessionIOError):
    """No such session: no registry entry, no log, or an id that cannot name one."""

    status = 404


class ControlUnavailable(SessionIOError):
    """The session exists but cannot be written to.

    A distinct status from "not found" because the operator's next move differs:
    the scrollback is still readable, and the reason (exited, crashed, spawned
    without a control plane) tells them whether waiting or respawning is the fix.
    """

    status = 409


class ControlPlaneError(SessionIOError):
    """The session's control plane was reachable in principle but did not answer.

    A gateway failure, not a client error: the request was well-formed and the
    platform is the one that could not complete it.

    ``delivered`` is the one thing a caller cannot work out from the message, and
    it decides what the caller may do next: **whether the payload reached the
    session before this was raised.** :func:`send_input` raises for two unrelated
    reasons and they need opposite recoveries — a refusal means nothing was typed
    and retrying is correct, while a receipt mismatch means "the payload WAS typed
    into the session" and retrying types it twice. That distinction lived only in
    two prose messages until issue #317's second review round, where a caller that
    could not read it retyped a reminder into a live session on every tick of a
    30-second loop. It defaults to ``False``, so the safe reading — nothing
    arrived, the operation can be retried — is what a raise site gets by saying
    nothing.
    """

    status = 502

    def __init__(self, *args, delivered: bool = False) -> None:
        super().__init__(*args)
        #: See the class docstring: read by callers that must not repeat a
        #: payload the session already has.
        self.delivered = delivered


@dataclass(frozen=True)
class LogChunk:
    """One slice of a session's PTY log, plus where it sat in the file.

    ``offset`` is the *resolved* start — a tail request asks with a negative
    offset and needs to be told where that landed, because the next read has to
    continue from there.

    ``source`` says which of a session's two logs the bytes and the offsets belong
    to (:func:`canonical_log`). Reported rather than kept private because it is the
    only thing that explains a discontinuity in a terminal: a client that saw its
    source change knows why the stream jumped, and an operator reading the route's
    JSON can tell "this session records itself" from "this session is only teed by
    the host" without going to look at the filesystem. It defaults to
    :data:`LOG_SOURCE_HOST`, which is what every session had before there was a
    second log.
    """

    offset: int
    next_offset: int
    size: int
    data: bytes
    source: str = LOG_SOURCE_HOST

    def to_dict(self) -> dict:
        """Wire form. ``encoding`` is stated so no client ever treats it as text."""
        return {
            "offset": self.offset,
            "next_offset": self.next_offset,
            "size": self.size,
            "encoding": "base64",
            "source": self.source,
            "data": base64.b64encode(self.data).decode("ascii"),
        }


@dataclass(frozen=True)
class ResizeReport:
    """What the control plane made of a resize, classified for the caller.

    Exists so the classification is testable without a socket: the WebSocket
    handler's only decision should be "forward this as a status frame", not "what
    does a 503 from /resize mean".
    """

    applied: bool
    status: Optional[int] = None
    event: Optional[str] = None
    message: Optional[str] = None


@dataclass(frozen=True)
class ControlOutput:
    """One slice of a session's *in-container* output, read over ``/output``.

    Deliberately not a :class:`LogChunk`: the two describe different streams in
    different offset spaces (see the module docstring), and a shared type is how
    a cursor from one ends up seeking in the other.

    ``cursor`` is the supervisor's next cursor, taken from its answer and never
    computed as ``cursor + len(data)`` — the route hands back a *decoded* string
    whose length is not the byte count the ring buffer advanced by.

    ``dropped`` is how many bytes the ring buffer evicted before the platform
    asked for them. It is a number of bytes that no longer exist anywhere: the
    container's buffer is bounded, and the host log never saw them because the
    drain that would have written them was gone. Reported rather than smoothed
    over, so a gap in a session's scrollback is visible as a gap.
    """

    data: bytes
    cursor: int
    dropped: int


@dataclass(frozen=True)
class ControlEndpoint:
    """Where and how to reach one session's control plane.

    ``token`` is excluded from the generated ``repr`` on purpose: this object
    exists to be passed around, and a dataclass that prints its own credential is
    one stray log line away from putting it in the journal.
    """

    host: str
    port: int
    token: str = field(repr=False)

    @property
    def location(self) -> str:
        """Host and port, safe to log and to put in an error message."""
        return f"{self.host}:{self.port}"

    def url(self, path: str) -> str:
        return f"http://{self.location}{path}"

    def auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}


@dataclass(frozen=True)
class _ControlReply:
    status: int
    payload: dict

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def detail(self) -> str:
        """Upstream explanation, truncated — a 422's ``detail`` is a whole list."""
        detail = self.payload.get("detail")
        return str(detail)[:_DETAIL_LIMIT] if detail else f"HTTP {self.status}"


def _validated(session_id: str) -> str:
    """Reject an id that would not be legal as a registry filename.

    Session ids arrive from a URL path and are about to be interpolated into one,
    and :func:`lmer_platform.spawn.log_path_for` does no checking of its own. So
    the check is borrowed from the registry rather than written again here: two
    different notions of a legal session id is how ``..`` eventually gets past
    one of them.
    """
    try:
        registry.session_path(session_id)
    except registry.RegistryError as exc:
        raise SessionNotFound(str(exc)) from exc
    return session_id


def session_log_path(session_id: str) -> Path:
    """Path of the host-side PTY log, with the id validated first."""
    return log_path_for(_validated(session_id))


def container_log_path(session_id: str) -> Path:
    """Path of the log the session wrote itself, with the id validated first.

    The path is where it *would* be: this says nothing about whether the file
    exists, which is the question :func:`canonical_log` asks and the only question
    that decides anything.
    """
    return container_log_path_for(_validated(session_id))


def canonical_log(session_id: str) -> tuple:
    """The log of record for *session_id*, as ``(path, source)``.

    The whole choice, in one place, made by probing a file and never by checking a
    version — see the module docstring for why that distinction is the feature
    rather than an implementation taste. A session whose image writes its own log
    is served that; every other session is served the host-side tee, exactly as
    before.

    The probe is "does it hold anything", not "is it there", and the difference is
    a blank terminal. The in-container log is created when the supervisor starts,
    which is *after* host-side ``lmer`` has printed the pull, the clone and its
    announce lines into the tee — so between those two moments an
    existence-only probe would serve a zero-byte file and show the operator an
    empty screen with the session's whole launch sitting in the other file. An
    empty log is a writer that has not recorded anything yet, and until it has, the
    tee is still the record. The same reading covers the failure directions for
    free: a session that dies before its harness draws a byte, and a log the writer
    abandoned by unlinking (``lmer_cli.supervisor.SessionLog.write``), both fall
    back to the complete copy.

    Which also settles what the terminal view shows for a modern session: the
    harness's own output, from its first byte, and *not* the host-side launch
    chatter — while a launch that failed before any harness ran has no
    in-container log at all, so exactly the sessions whose scrollback needs to
    show the failure still get the tee.

    Callers that will do several reads for one client resolve this **once** and
    reuse it, so a cursor's meaning cannot change mid-stream.
    """
    container = container_log_path(session_id)
    try:
        if container.stat().st_size > 0:
            return container, LOG_SOURCE_CONTAINER
    except OSError:
        # Absent is the ordinary answer (an older image, or a container that has
        # not started yet); unreadable is rarer and means the same thing here —
        # the tee is what is left to serve.
        pass
    return session_log_path(session_id), LOG_SOURCE_HOST


def named_log(session_id: str, source: str) -> tuple:
    """The log a caller asked for **by name**, as ``(path, source)``.

    The escape hatch from :func:`canonical_log`, and deliberately a narrow one: it
    answers "that log", not "the log of record". One caller wants it — the
    terminal, showing the host-side launch of a session whose own log is the
    record. That output is real, it is on disk, and once the in-container log
    exists nothing that reads the canonical source can ever reach it: the pull,
    the clone and lmer's own announce lines were printed before the container had
    a log to write into, so paging back through the canonical log stops at the
    harness's first byte and calls it the beginning.

    What this does **not** do is stitch the two together (see the module
    docstring). It serves one named file per read, in that file's own offset
    space, and the source travels back on the chunk so a caller cannot lose track
    of which space it is holding a number in. A cursor from here must never be
    handed to :func:`follow_log`, which resolves the canonical source for itself.

    A name that is neither log is :class:`SessionNotFound` rather than a value
    error: from the caller's side it named a log this session does not have.
    """
    if source == LOG_SOURCE_CONTAINER:
        return container_log_path(session_id), LOG_SOURCE_CONTAINER
    if source == LOG_SOURCE_HOST:
        return session_log_path(session_id), LOG_SOURCE_HOST
    raise SessionNotFound(
        f"no log source named {source!r}: a session has "
        f"{LOG_SOURCE_CONTAINER!r} (its own) and {LOG_SOURCE_HOST!r} (the tee)"
    )


def require_session(session_id: str) -> Optional[dict]:
    """The session's registry entry, or ``None`` for one that is only a log.

    Raises :class:`SessionNotFound` when neither exists. The ``None`` case is the
    point of the whole module: a session that exited cleanly has no entry, and
    refusing to serve its scrollback would throw away the record of everything it
    did.

    *Either* log is evidence that a session existed. Asking only about the host tee
    would refuse to serve a session whose own log is sitting right there — the case
    an operator hits after pruning the logs directory unevenly, and a 404 there
    would read as "that session never happened".
    """
    try:
        entry = registry.read_session(session_id)
    except registry.RegistryError as exc:
        raise SessionNotFound(str(exc)) from exc
    if entry is not None:
        return entry
    if session_log_path(session_id).exists() or container_log_path(session_id).exists():
        return None
    raise SessionNotFound(
        f"no such session {session_id!r}: it has neither a registry entry nor a "
        "PTY log on this host"
    )


def session_is_live(session_id: str) -> bool:
    """Whether the session's own process still exists."""
    try:
        return registry.is_live(registry.read_session(session_id))
    except registry.RegistryError:
        return False


def read_log_at(
    path: Path,
    offset: int,
    limit: int = DEFAULT_LOG_LIMIT,
    *,
    source: str = LOG_SOURCE_HOST,
) -> LogChunk:
    """Read *limit* bytes of *path* from *offset*, resolving a negative offset.

    A negative offset means "the last N bytes", like ``tail -c``: an attaching
    terminal wants the end of a log that may be hundreds of megabytes, and asking
    for it by absolute offset would cost a round trip just to learn the size.

    Size and content come from one open handle so the offsets returned describe
    the bytes returned even while the drain thread is appending. A log that
    *shrank* (truncated out from under us) resolves to its new end rather than
    reading forever past it — and that same clamp is what a cursor measured in a
    session's *other* log lands on, which the module docstring explains is the
    lesser of the two available wrongs.

    *source* only labels what is returned; it does not select the file, because a
    caller that resolved a path already resolved the source with it
    (:func:`canonical_log`). It defaults to the host tee, which is where every
    caller of this function read from before there was a choice.

    The limit is clamped here as well as at the HTTP boundary, because the
    WebSocket path has no FastAPI validation between the client and this call.
    """
    limit = max(1, min(int(limit), MAX_LOG_LIMIT))
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            start = size + offset if offset < 0 else offset
            start = max(0, min(start, size))
            handle.seek(start)
            data = handle.read(limit)
    except FileNotFoundError:
        # A session registered a moment ago has no log until its first byte is
        # drained. An empty scrollback is the truthful answer; a 404 would say
        # the session does not exist.
        return LogChunk(offset=0, next_offset=0, size=0, data=b"", source=source)
    except OSError as exc:
        raise SessionIOError(f"cannot read the log for this session ({exc})") from exc
    return LogChunk(
        offset=start,
        next_offset=start + len(data),
        size=size,
        data=data,
        source=source,
    )


def read_log(
    session_id: str,
    *,
    offset: int = 0,
    limit: int = DEFAULT_LOG_LIMIT,
    source: Optional[str] = None,
) -> LogChunk:
    """Scrollback for one session, live or long gone.

    *source* names one of the session's two logs (:func:`named_log`) instead of
    taking whichever is canonical. ``None`` — every caller that does not care, and
    every caller there was before this parameter — reads the log of record, which
    is the only stream a cursor may be carried across.

    A log that does not exist reads as empty rather than as an error, which is
    what :func:`read_log_at` already does for a session whose first byte has not
    been drained yet: asking for the host tee of a session that never had one is
    a truthful "nothing here", not a 404 on the session.
    """
    require_session(session_id)
    if source is None:
        path, resolved = canonical_log(session_id)
    else:
        path, resolved = named_log(session_id, source)
    return read_log_at(path, offset, limit, source=resolved)


async def follow_log(
    session_id: str,
    *,
    offset: int = 0,
    limit: int = DEFAULT_LOG_LIMIT,
    poll: Optional[float] = None,
) -> AsyncIterator[LogChunk]:
    """Yield the backlog from *offset*, then new bytes as they are drained.

    Ends — rather than blocking forever — once the session's process is gone and
    the log has been read out, so an attached client learns the session finished
    instead of watching a silent socket. That ordering matters: liveness is
    checked only after a read came back empty, and then the log is drained *once
    more* after a grace tick, because the tee thread can still be flushing the
    final bytes of a process that has already been reaped.

    Polling rather than inotify: the alternative is a platform-specific watcher
    for bytes that, on the common path, are being written by a thread in this very
    process — and the poll is cheap enough to run fast enough to feel live (see
    :data:`POLL_INTERVAL_SECONDS`).

    *poll* falls back to :data:`POLL_INTERVAL_SECONDS` read at call time, so tests
    can shorten the interval without threading a parameter through every caller.

    The source is resolved **once**, before the first read, and held for the life
    of the stream: the offsets this yields are what the client hands back on
    reconnect, so a source that changed halfway would silently redefine the cursor
    of an attached terminal. A session that starts writing its own log while
    somebody is watching is therefore served the host tee until that client comes
    back — which loses nothing, since the tee is alive for as long as the daemon
    holding this stream open is.
    """
    interval = POLL_INTERVAL_SECONDS if poll is None else poll
    require_session(session_id)
    path, source = canonical_log(session_id)
    cursor = offset

    while True:
        chunk = await asyncio.to_thread(read_log_at, path, cursor, limit, source=source)
        cursor = chunk.next_offset
        if chunk.data:
            yield chunk
            continue
        if session_is_live(session_id):
            await asyncio.sleep(interval)
            continue
        await asyncio.sleep(interval)
        while True:
            final = await asyncio.to_thread(
                read_log_at, path, cursor, limit, source=source
            )
            cursor = final.next_offset
            if not final.data:
                return
            yield final


def control_endpoint(session_id: str) -> ControlEndpoint:
    """Resolve where to write into *session_id*, or explain why nothing can.

    Liveness is checked before the port is even read: a crashed session keeps its
    entry (that is how the crash stays visible) and its token, so without this
    check the operator's answer would fail as a refused connection and read as a
    platform bug rather than as "that session died".
    """
    entry = require_session(session_id)
    if entry is None:
        raise ControlUnavailable(
            f"session {session_id} has exited — its log can still be read, but "
            "there is no process left to type into"
        )
    if not registry.is_live(entry):
        raise ControlUnavailable(
            f"session {session_id} is not running (its process is gone) — its "
            "log is still readable, and the entry is kept as the crash signal"
        )

    control = entry.get("control")
    port = control.get("port") if isinstance(control, dict) else None
    if not isinstance(port, int) or isinstance(port, bool) or port <= 0:
        raise ControlUnavailable(
            f"session {session_id} has no control plane in its registry entry, "
            "so it cannot be written to. Only sessions the platform spawned "
            "(always with --fastapi) can be"
        )
    credential = read_control_token(session_id)
    if not credential:
        raise ControlUnavailable(
            f"session {session_id}'s control-plane token is missing or "
            "unreadable, so the platform cannot authenticate to it"
        )
    host = control.get("host") or _DEFAULT_CONTROL_HOST
    return ControlEndpoint(host=str(host), port=port, token=credential)


def _call(
    endpoint: ControlEndpoint,
    method: str,
    path: str,
    *,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
    timeout=CONTROL_TIMEOUT_SECONDS,
) -> _ControlReply:
    """One control-plane call, with the token in a header and never in the URL.

    A transport failure raises; an HTTP error is returned for the caller to
    classify, because ``/resize`` treats some of those as "fine, carry on" and
    ``/input`` treats all of them as failure.

    ``requests`` echoes the URL it was given into its exception messages, which
    is why the credential travels as a header — the same leak
    ``clone_and_exec._scrub_credentials`` exists to fix (MR !104). That the
    message is credential-free here is a property of the URL, so it is checked
    rather than assumed.

    *timeout* is passed to ``requests`` untouched, so a caller may hand it the
    ``(connect, read)`` pair. The long-poll read of ``/output`` needs exactly
    that: its read budget is *tens of seconds* by design, while a connect to
    loopback that has not completed in the usual few seconds is a control plane
    that is gone, and waiting the read budget to find that out would make a dead
    session look busy.
    """
    try:
        response = requests.request(
            method,
            endpoint.url(path),
            json=body,
            params=params,
            headers=endpoint.auth_headers(),
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise ControlPlaneError(
            f"cannot reach the session's control plane at {endpoint.location} "
            f"({exc})"
        ) from exc

    text = response.text
    if endpoint.token in text:
        # Whatever the control plane says is relayed to the client, so the
        # session's own credential must not be able to ride along in it. The
        # supervisor does not echo request headers today; this keeps a version
        # that did from turning an error body into a disclosure.
        text = text.replace(endpoint.token, "<redacted>")
    try:
        payload = json.loads(text)
    except ValueError:
        payload = {"detail": text[:_DETAIL_LIMIT]}
    if not isinstance(payload, dict):
        payload = {"detail": str(payload)[:_DETAIL_LIMIT]}
    return _ControlReply(status=response.status_code, payload=payload)


def _post(
    endpoint: ControlEndpoint,
    path: str,
    body: dict,
    *,
    timeout=CONTROL_TIMEOUT_SECONDS,
) -> _ControlReply:
    """A write to the control plane. See :func:`_call` for the shared mechanics."""
    return _call(endpoint, "POST", path, body=body, timeout=timeout)


def _get(
    endpoint: ControlEndpoint,
    path: str,
    *,
    params: Optional[dict] = None,
    timeout=CONTROL_TIMEOUT_SECONDS,
) -> _ControlReply:
    """A read from the control plane. See :func:`_call` for the shared mechanics.

    The query string is built by ``requests`` from *params* rather than being
    interpolated into the path: a cursor is an integer the platform computed, but
    the habit of formatting values into a URL is how the *token* eventually ends
    up in one, and there is no second place here where that decision gets made.
    """
    return _call(endpoint, "GET", path, params=params, timeout=timeout)


def _payload_hmac(
    endpoint: Optional[ControlEndpoint], payload_bytes: bytes
) -> Optional[str]:
    """The durable form of "what was sent": an HMAC, never a bare hash.

    The events log is append-only and never pruned, and short payloads make a
    raw SHA-256 a dictionary lookup — a one-word answer has few enough
    candidates that logging its hash IS logging its content. Keying with the
    session's control token makes the record uninvertible from the log alone,
    and checkable only while the session lives: the token file is unlinked at
    registry removal while the events log is never pruned, so after teardown
    the field verifies nothing — which is the point, since the log outliving
    the key is what keeps old entries from ever becoming content. ``None``
    when endpoint resolution failed — there is no key, and the attempt record
    still carries the length and the error.
    """
    if endpoint is None:
        return None
    return hmac.new(
        endpoint.token.encode("utf-8"), payload_bytes, hashlib.sha256
    ).hexdigest()


def _record_attempt(
    event_type: str,
    session_id: str,
    *,
    endpoint: Optional[ControlEndpoint],
    reply: Optional[_ControlReply],
    error: Optional[str],
    **fields,
) -> None:
    """One INFO line + one events-log entry per control-plane attempt.

    Called from a ``finally`` so the record exists for every attempt, not only
    the ones that got an HTTP answer: the first resize against a starting
    session routinely dies as a connection reset, and a log in which "no
    record" reads as "no attempt" is exactly the silence #197 exists to
    remove. ``status`` is ``None`` when no HTTP answer arrived and ``error``
    says what happened instead; ``endpoint`` is ``None`` when resolution
    itself was the failure. An HTTP *refusal* fills ``error`` too, with the
    upstream detail — a 500's "pty is gone" existing only in an exception
    string nothing writes down would be the same silence with a status code.
    """
    location = endpoint.location if endpoint is not None else None
    status = reply.status if reply is not None else None
    if error is None and reply is not None and not reply.ok:
        error = reply.detail
    logger.info(
        "platform_%s session=%s endpoint=%s status=%s error=%s %s",
        event_type, session_id, location, status, error,
        " ".join(f"{key}={value}" for key, value in fields.items()),
    )
    append_event(
        event_type,
        data={
            "session": session_id,
            "endpoint": location,
            "status": status,
            "error": error,
            **fields,
        },
    )


def send_input(
    session_id: str, data: str, *, append_newline: bool = False,
    sanitize: bool = False, preserve_slash_commands: bool = False,
) -> dict:
    """Type *data* into a running session. Returns the control plane's answer.

    Loud by design: everything that can go wrong raises. This is the path an
    operator uses to answer a session that stopped to ask a question, and an
    answer that quietly went nowhere leaves them believing the session was
    unblocked while it sits there waiting.

    *append_newline* defaults off, matching the control plane it forwards to — a
    caller that means "and press Enter" says so. The payload itself is never
    logged: an answer routinely contains whatever the operator was asked for.

    *sanitize* says the payload is prose intended to steer the session, from
    either the chat composer or ``lmer-ctl send``. That lets the supervisor
    defuse shapes a TUI reads as a command rather than as text
    (``lmer_cli.supervisor._sanitize_user_chat``). Off by default and only put
    on the wire when set: raw terminal keystrokes and injected lifecycle
    commands send exactly the body they sent before the flag existed.

    *preserve_slash_commands* narrows that transformation for an orchestration
    caller whose leading slash is intentional. It has no effect unless
    *sanitize* is also set; the supervisor remains the owner of the harness's
    escape set and leaves only slash commands out of the prose guard.

    The reply carries ``submit_confirmed`` and a ``note`` when Enter was asked
    for, and both are passed to the caller rather than dropped: the supervisor
    writes the CR to the PTY and cannot see whether the TUI registered it as a
    submit, and "loud by design" has to include the part that is uncertain. See
    ``lmer_cli.supervisor._SUBMIT_UNCONFIRMED_NOTE``.
    """
    if not isinstance(data, str):
        raise SessionIOError(
            f"input data must be a string, got {type(data).__name__}"
        )
    # Hashed before the wire so the receipt is independent of everything past
    # this line: the control plane answers with the hash of what *it* received
    # (``payload_sha256``, #197), and the two agreeing is what "delivered
    # intact" means — including under *sanitize*, which the supervisor applies
    # after taking that hash, so the receipt keeps saying what crossed the wire
    # rather than what was typed. The raw hash lives only in memory for that
    # comparison — what is *recorded* is an HMAC (:func:`_payload_hmac` has why).
    payload_bytes = data.encode("utf-8")
    sent_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    body = {"data": data, "append_newline": bool(append_newline)}
    if sanitize:
        body["sanitize"] = True
        if preserve_slash_commands:
            body["preserve_slash_commands"] = True
    endpoint: Optional[ControlEndpoint] = None
    reply: Optional[_ControlReply] = None
    error: Optional[str] = None
    try:
        endpoint = control_endpoint(session_id)
        reply = _post(endpoint, "/input", body)
    except SessionIOError as exc:
        error = str(exc)[:_DETAIL_LIMIT]
        raise
    finally:
        if append_newline:
            receipt = (
                reply.payload.get("payload_sha256") if reply is not None else None
            )
            _record_attempt(
                "session_input", session_id,
                endpoint=endpoint, reply=reply, error=error,
                bytes=reply.payload.get("bytes_written") if reply else None,
                length=len(payload_bytes),
                payload_hmac=_payload_hmac(endpoint, payload_bytes),
                # A verdict, not the receipt itself: the supervisor's receipt
                # is a raw hash, and writing it durably would undo what the
                # HMAC above is for. ``None`` = no receipt to compare (old
                # control plane, or no answer at all).
                receipt_match=(
                    None if receipt is None else receipt == sent_sha256
                ),
                submit_text=reply.payload.get("submit_text") if reply else None,
            )
        else:
            # A write with no Enter is the web terminal's per-keystroke path —
            # one call per character typed. Durably recording those would grow
            # the events log without bound AND reconstruct typed input
            # character by character (including at hidden prompts), so the
            # delivery-forensics contract (#197) covers *messages*; keystrokes
            # get a debug line and are otherwise what the PTY already echoes.
            logger.debug(
                "platform_session_keystroke session=%s endpoint=%s status=%s "
                "bytes=%s error=%s",
                session_id,
                endpoint.location if endpoint else None,
                reply.status if reply else None,
                reply.payload.get("bytes_written") if reply else None,
                error,
            )
    if not reply.ok:
        raise ControlPlaneError(
            f"session {session_id} refused the input ({reply.detail})"
        )
    receipt_sha256 = reply.payload.get("payload_sha256")
    if receipt_sha256 is not None and receipt_sha256 != sent_sha256:
        # The control plane read back different bytes than were sent. Never
        # observed (delivery was proven byte-perfect in #236's investigation),
        # but this is the check that turns "we believe the wire is clean" into
        # a receipt — and a mismatch must be loud, not a healthy-looking 200.
        # Loud AND accurate: the 200 means the bytes were already typed into
        # the session, so this must not read as a refusal — the taught
        # recovery for "not sent" is retyping, which would deliver it twice.
        # ``delivered`` carries the same fact in a form a caller can branch on;
        # the sentence below is for a human.
        raise ControlPlaneError(
            f"session {session_id} received the input, but its control plane "
            f"acknowledged different bytes than were sent (sent sha256 "
            f"{sent_sha256}, control plane read {receipt_sha256}) — the "
            "payload WAS typed into the session; check the terminal view "
            "before retyping anything",
            delivered=True,
        )
    return reply.payload


def apply_resize(session_id: str, rows: int, cols: int) -> ResizeReport:
    """Tell a session's PTY how big the client's terminal is. Best-effort.

    Three answers from the control plane are not failures of this call and must
    never cost the caller its socket:

    - **404** — whatever answers on the session's control port has no
      ``/resize`` route. The 404 alone does not say why, and this code used to
      think it did: the docstring once read "the session's image predates the
      route", and #236 disproved it — sessions from a current image answered
      404 because their supervisor had imported a stale checkout (the self-dev
      ``/workspace`` clone of ``main``). An old image, a stale supervisor and
      a re-bound port all produce this identical answer, which is why the
      report states only the observation and names the endpoint; the
      endpoint's ``/healthz`` ``source`` field is the checkable fact for
      whoever investigates.
    - **503** — the supervisor is running with no PTY hook wired, so there is
      nothing to resize. A deployment fact, as its own docstring says.
    - **500** — the ioctl failed, which in practice means the PTY is gone and the
      session is on its way out. Reported rather than swallowed, because that is
      the one of the three the client can act on: it is about to end.

    A session that cannot be reached *at all* still raises
    (:class:`ControlUnavailable`, :class:`ControlPlaneError`) — that condition is
    identical for input, and the caller handles it in one place.

    The upper bounds on the geometry belong to the control plane, which owns the
    PTY; re-stating them here would just be a second place to keep them in sync.
    The *lower* bound below is not a re-statement of anything: the control plane
    accepts 1..1000 by contract (and deployed images cannot be amended), but no
    real terminal proposes a handful of columns — such a value is a measurement
    artifact, and one reflowed a live session's TUI to a single column when a
    client fit against a mid-animation sliver layout and clamped the result into
    legality (2026-07-29). The daemon is the one writer every client shares, so
    the daemon refuses.
    """
    if rows < MIN_RESIZE_ROWS or cols < MIN_RESIZE_COLS:
        return ResizeReport(
            applied=False,
            event="resize_refused",
            message=(
                f"refused a {cols}x{rows} resize — no real terminal is that "
                "small, so this reading is a layout artifact, not a window size"
            ),
        )
    endpoint: Optional[ControlEndpoint] = None
    reply: Optional[_ControlReply] = None
    error: Optional[str] = None
    try:
        endpoint = control_endpoint(session_id)
        reply = _post(endpoint, "/resize", {"rows": rows, "cols": cols})
    except SessionIOError as exc:
        error = str(exc)[:_DETAIL_LIMIT]
        raise
    finally:
        # Every attempt, at INFO, with the endpoint the daemon actually dialed
        # — including the ones that got no HTTP answer at all: #236's 404 went
        # unexplained for days partly because the only record of a failing
        # resize was a debug line that named neither the endpoint nor the
        # status, under a message asserting a cause the incident disproved.
        _record_attempt(
            "session_resize", session_id,
            endpoint=endpoint, reply=reply, error=error,
            rows=rows, cols=cols,
        )
    if reply.ok:
        return ResizeReport(applied=True, status=reply.status)
    if reply.status == 404:
        # An unknown-route answer. Reported as exactly that — an observation.
        # #236 taught this line twice over: the old message asserted "the
        # image predates the route" and was wrong (the supervisor had imported
        # a stale checkout), and any replacement diagnosis would be the same
        # mistake with different words — a re-bound port, for one, produces
        # this identical answer. The checkable fact lives at the endpoint's
        # /healthz ``source``; the docstring carries the #236 history.
        logger.warning(
            "platform_session_resize_route_missing session=%s endpoint=%s — "
            "the endpoint answered 404 for /resize: whatever code serves that "
            "port has no such route (its /healthz `source` names the file)",
            session_id, endpoint.location,
        )
        return ResizeReport(
            applied=False,
            status=reply.status,
            event="resize_unsupported",
            message=(
                "this session's control plane does not serve /resize "
                f"(HTTP 404 from {endpoint.location}: {reply.detail}) — "
                "rendering continues at its current size"
            ),
        )
    if reply.status == 503:
        return ResizeReport(
            applied=False,
            status=reply.status,
            event="resize_unsupported",
            message=(
                "this session cannot be resized "
                f"({reply.detail}) — rendering continues at its current size"
            ),
        )
    logger.warning(
        "platform_session_resize_failed session=%s status=%s detail=%s",
        session_id, reply.status, reply.detail,
    )
    return ResizeReport(
        applied=False,
        status=reply.status,
        event="resize_failed",
        message=(
            f"resize failed ({reply.detail}) — the session's terminal is "
            "probably gone, which usually means it is ending"
        ),
    )


def probe_health(session_id: str) -> dict:
    """Ask a session's control plane whether it is there. Loud on every failure.

    The one call that distinguishes "the platform lost its view of this session"
    from "this session is gone", which are indistinguishable from the host log
    alone — the log stops growing either way. So this raises rather than
    returning a "no": a caller about to claim a session is alive must not be able
    to do so by ignoring a return value.

    The answer carries the supervisor's ``cursor`` (its ring buffer's current end
    offset) and the PTY's ``rows``/``cols``. Both are why a re-attach probes at
    all rather than just opening ``/output``: the cursor is where a reader must
    start to avoid re-reading output the host log already holds, and a geometry
    of ``0x0`` — which is what a session spawned with no host terminal reports —
    is the signal that nothing has ever told this PTY how big it is.
    """
    endpoint = control_endpoint(session_id)
    reply = _get(endpoint, "/healthz")
    if not reply.ok:
        raise ControlPlaneError(
            f"session {session_id}'s control plane answered "
            f"{reply.status} to a health probe ({reply.detail})"
        )
    return reply.payload


def _quiet_health(session_id: str) -> Optional[dict]:
    """One cheap health probe, or ``None`` for every way of not getting an answer.

    The shared half of :func:`session_activity` and :func:`control_plane_answers`:
    both ask the same route on the same budget (:data:`ACTIVITY_TIMEOUT_SECONDS`)
    and differ only in what they read off the answer, so two copies would be two
    places for the timeout and the log level to drift apart.

    Quiet, unlike :func:`probe_health` on the same route — see that function for
    why the loud one exists. Every failure is one answer here: unreachable,
    refused, too slow, never spawned with a control plane.
    """
    try:
        endpoint = control_endpoint(session_id)
        reply = _get(endpoint, "/healthz", timeout=ACTIVITY_TIMEOUT_SECONDS)
    except SessionIOError as exc:
        # Debug, not a warning: an unreachable control plane is the loud business
        # of the paths that need one (:func:`send_input`, re-attach), and a fleet
        # view polled every few seconds would turn one dead container into a log
        # nobody can read.
        logger.debug(
            "platform_session_health_unavailable session=%s error=%s",
            session_id, exc,
        )
        return None
    if not reply.ok:
        logger.debug(
            "platform_session_health_refused session=%s status=%s",
            session_id, reply.status,
        )
        return None
    return reply.payload


def control_plane_answers(session_id: str) -> bool:
    """Whether *session_id*'s control plane is answering **right now**.

    A liveness question about the *container*, which the registry cannot answer:
    the entry's pid is the host-side ``lmer``, and that process outlives its
    container by however long its teardown takes — minutes of run-state commits,
    with the in-container supervisor and everything it was supervising already
    gone. So a caller that needs to know whether anything is left inside has to
    ask inside, and this is the cheap way to (:data:`ACTIVITY_TIMEOUT_SECONDS`,
    one loopback request that is answered from memory).

    Deliberately *not* a statement about the harness's health — the supervisor
    answers ``/healthz`` for as long as it is up, and this only says that
    something in there did. It is the strongest evidence available about the
    other side of a mount (:func:`lmer_platform.ask.answer_question`), and it is
    read in that direction only: an answer means the container is there, and no
    answer means nothing can be relied on to read what is written into it.

    Quiet like :func:`session_activity` and for the same reason its docstring
    gives, with one difference in what a caller may conclude: a ``False`` here is
    ambiguous by construction (an old image still answers ``/healthz``, so this
    is not the mixed-fleet trap the activity fields are, but a wedged container
    and a gone one look identical), so callers must treat it as "no reader can be
    proved" rather than as "the container is gone".
    """
    return _quiet_health(session_id) is not None


def session_activity(session_id: str) -> Optional[dict]:
    """How long *session_id*'s harness has been quiet, or ``None`` if unknowable.

    The counterpart to :func:`probe_health` on the same route, and deliberately
    its opposite in temperament. That one is *loud* because its caller is about to
    claim a session is alive; this one is **quiet on every failure**, because its
    caller is assembling a fleet view and the fact is a decoration on a row. A row
    that lost its idle reading still says everything it said before this existed;
    a fleet view that raised because one container did not answer says nothing at
    all.

    Why the fact has to come over the wire at all: run state moves when a session
    *ends* (spec D24), so a run that finished its work and is sitting at its prompt
    reads exactly like one that is still working, and the only process that knows
    the difference is the supervisor holding the PTY inside the container
    (:class:`lmer_cli.supervisor.OutputBuffer`).

    Returns the record as the supervisor reported it —
    ``{"last_output_at": <iso|None>, "idle_seconds": <number>}`` — or ``None`` for
    every way of not knowing, of which there are three and all are ordinary:

    - **the session's image predates this** and answers a health probe with no
      activity fields at all. That is the mixed fleet, and it must read exactly as
      it did before the fields existed rather than as ``idle 0s``;
    - **the container did not answer** — unreachable, refused, too slow
      (:data:`ACTIVITY_TIMEOUT_SECONDS`), not spawned with a control plane;
    - **the reading is not usable**. ``bool`` is an ``int`` subclass, so a JSON
      ``true`` would otherwise become "idle for one second" — the same check
      :func:`read_control_output` makes of its cursor — and a negative idle is a
      clock nobody should render.

    ``last_output_at`` rides along only when it is a string, because it is the
    tooltip and the loggable form of a measurement that ``idle_seconds`` *is*: a
    record with the timestamp and no number would have nothing to render.
    """
    return _activity_of(_quiet_health(session_id))


def _activity_of(payload: Optional[dict]) -> Optional[dict]:
    """:func:`session_activity`'s rules, applied to a health payload already read.

    Split out so :func:`session_output_state` can answer a second question about
    the same payload without a second call into the container.
    """
    if payload is None:
        return None
    idle = payload.get("idle_seconds")
    if isinstance(idle, bool) or not isinstance(idle, (int, float)) or idle < 0:
        return None
    last_output_at = payload.get("last_output_at")
    return {
        "last_output_at": last_output_at if isinstance(last_output_at, str) else None,
        "idle_seconds": idle,
    }


def session_output_state(session_id: str) -> Optional[dict]:
    """``{"idle_seconds": …, "produced": …}`` for a session, or ``None``.

    :func:`session_activity` plus the one fact it deliberately drops, in the same
    request: **has this session ever produced output at all.** ``None`` for the
    whole record means the container did not answer — the same silence
    :func:`session_activity` reports, and for the same reason.

    Why the second fact needs a home (issue #317's review). ``idle_seconds`` is
    ``None`` in three situations that
    :func:`session_activity` collapses into one, because a fleet row renders them
    identically: an image that predates the activity fields, a plane that did not
    answer, and **a session whose harness has not drawn a byte yet** — the last of
    which the supervisor reports as an explicit null
    (:class:`lmer_cli.supervisor.OutputBuffer`). A caller about to *type into* the
    session cannot treat that third case as unknowable: there is no TUI reading
    yet, so the bytes go nowhere. The distinction survives at the source, in a
    field the same payload already carries — ``cursor``, the offset just past the
    last byte ever written — so it is read here rather than inferred from timing.

    ``produced`` is therefore three-valued on purpose: ``True`` (bytes have been
    drawn), ``False`` (the plane answered and none ever have), ``None`` (the plane
    answered but said nothing this can read — an older build with no ``cursor``).
    Only ``False`` is a fact a caller may act on; ``None`` keeps whatever the
    caller does with "not knowable".
    """
    payload = _quiet_health(session_id)
    if payload is None:
        return None
    cursor = payload.get("cursor")
    produced = None
    if isinstance(cursor, int) and not isinstance(cursor, bool) and cursor >= 0:
        produced = cursor > 0
    return {
        "idle_seconds": (_activity_of(payload) or {}).get("idle_seconds"),
        "produced": produced,
    }


def read_control_output(
    session_id: str, *, cursor: int, timeout: float = 0.0
) -> ControlOutput:
    """Read a session's in-container output past *cursor*, over the control plane.

    The read half of the path :func:`send_input` already uses, and the only way
    to see a session whose host PTY died with the daemon that owned it.

    *timeout* is the supervisor's own long poll: it blocks up to that many
    seconds waiting for a byte rather than answering empty, so a caller polls at
    whatever cadence it likes without paying a request per idle tick. The HTTP
    read budget is that plus :data:`CONTROL_TIMEOUT_SECONDS`, because a server
    that is *deliberately* holding the request open for twenty seconds must not
    be timed out at five — the grace covers the answer's own round trip while
    still bounding a control plane that stopped answering mid-poll.

    ``data`` comes back as text and is re-encoded here, which is lossy in exactly
    one direction: see the module docstring on ``errors="replace"``. It is
    re-encoded rather than left as a string because the only consumer appends it
    to a log of raw terminal bytes, and a caller that had to remember to encode
    is a caller that will one day write ``str`` into a binary file.
    """
    endpoint = control_endpoint(session_id)
    reply = _get(
        endpoint,
        "/output",
        params={"cursor": int(cursor), "timeout": float(timeout)},
        timeout=(CONTROL_TIMEOUT_SECONDS, float(timeout) + CONTROL_TIMEOUT_SECONDS),
    )
    if not reply.ok:
        raise ControlPlaneError(
            f"session {session_id}'s control plane refused an output read "
            f"({reply.detail})"
        )
    payload = reply.payload
    data = payload.get("data")
    next_cursor = payload.get("cursor")
    # ``bool`` is an ``int`` subclass, so a JSON ``true`` would otherwise be
    # accepted as the cursor 1 and quietly rewind the reader to the start of the
    # container's buffer — the same check the registry makes of a pid.
    if (
        not isinstance(data, str)
        or not isinstance(next_cursor, int)
        or isinstance(next_cursor, bool)
    ):
        # A malformed answer is a gateway failure, not silently zero bytes: the
        # caller's next move is to stop appending to the log, and a fabricated
        # empty chunk would instead read as "the session went quiet".
        raise ControlPlaneError(
            f"session {session_id}'s control plane answered an output read with "
            f"no usable 'data'/'cursor' (got keys {sorted(payload)})"
        )
    dropped = payload.get("dropped_bytes")
    return ControlOutput(
        data=data.encode("utf-8"),
        cursor=next_cursor,
        dropped=dropped if isinstance(dropped, int) and dropped > 0 else 0,
    )


class TicketStore:
    """Short-lived, single-use tickets that authorize one WebSocket handshake.

    See the module docstring for why the socket cannot just present the shared
    secret. The three properties that make a ticket in a URL acceptable when the
    secret would not be:

    - **Bound to one session.** A ticket for session A cannot open session B's
      socket, so a leaked one does not become a key to the fleet.
    - **Redeemable once.** Replay from a log or a browser history is dead on
      arrival.
    - **Expiring in seconds**, measured on :func:`time.monotonic` so a wall-clock
      correction cannot extend a ticket's life (or expire every live one at once).

    Tickets are stored by SHA-256 digest, for the same reason a password file
    holds hashes: the daemon has no need to hold the plaintext after handing it
    out, and a lookup keyed on the digest cannot be turned into a comparison
    oracle on the ticket itself.

    A mismatched session id burns the ticket rather than leaving it redeemable.
    That costs a client that reconnected to the wrong id one extra round trip,
    and denies an attacker the ability to probe session ids with a single ticket.
    """

    def __init__(
        self,
        *,
        ttl: Optional[float] = None,
        capacity: int = MAX_LIVE_TICKETS,
    ) -> None:
        self.ttl = TICKET_TTL_SECONDS if ttl is None else ttl
        self._capacity = capacity
        self._lock = threading.Lock()
        self._issued: dict[str, tuple[str, float]] = {}

    @staticmethod
    def _digest(raw: str) -> str:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _sweep(self, now: float) -> None:
        for digest in [d for d, (_, exp) in self._issued.items() if exp <= now]:
            del self._issued[digest]

    def mint(self, session_id: str) -> str:
        """Issue a ticket for *session_id*.

        Swept on the way in rather than on a timer: the only moment the store can
        grow is a mint, so that is the only moment it needs pruning, and the
        daemon gets no background task to own.
        """
        now = time.monotonic()
        raw = secrets.token_urlsafe(32)
        with self._lock:
            self._sweep(now)
            while len(self._issued) >= self._capacity:
                oldest = min(self._issued, key=lambda d: self._issued[d][1])
                del self._issued[oldest]
                logger.warning(
                    "platform_tty_ticket_evicted capacity=%d — a client is "
                    "minting tickets it never redeems", self._capacity,
                )
            self._issued[self._digest(raw)] = (session_id, now + self.ttl)
        return raw

    def redeem(self, raw: str, session_id: str) -> bool:
        """Consume a ticket. ``False`` for anything unusable, without saying why.

        The caller gets one bit on purpose: an unauthenticated peer must not be
        able to tell "expired" from "wrong session" from "never existed", since
        those answers differ exactly by information about sessions it has not
        proved it may see.
        """
        if not raw:
            return False
        now = time.monotonic()
        with self._lock:
            self._sweep(now)
            issued = self._issued.pop(self._digest(raw), None)
        return issued is not None and issued[0] == session_id

    def live_count(self) -> int:
        """Unredeemed, unexpired tickets — for tests and for a health readout."""
        now = time.monotonic()
        with self._lock:
            self._sweep(now)
            return len(self._issued)
