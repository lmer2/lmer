"""Recovering a session whose host PTY died with the daemon (issue #141, T36).

What actually breaks when the daemon restarts
---------------------------------------------
The platform runs each session as ``lmer`` in a PTY it opened on the host, and a
thread tees that PTY master into ``logs/<session>.log`` — the scrollback the UI
renders (spec D16). Sessions are children but not dependants: they are started
with ``start_new_session=True`` and are meant to outlive the daemon (spec R11).

The *master fd* does not. It is owned by the daemon process, so it dies with it,
and no successor can re-open it — a pty master is an fd, not a path. The drain
thread goes with it, nothing appends to the log any more, and the UI faithfully
renders a file that stopped growing.

This was diagnosed the expensive way. A session looked wedged after a daemon
restart: full scrollback, no progress. Two probes settled it — ``/healthz``
answered, and the supervisor's ``cursor`` kept climbing across samples twenty
seconds apart. Nothing was wedged. The agent had worked the whole time; what died
was the platform's view of it. Input still worked, because input never touched
the PTY: it goes over the control plane, which is a process in the container that
a daemon restart does not touch.

That asymmetry — writes over HTTP, reads off an fd — *is* the bug. This module is
the missing half: it teaches reads to fall back to the path writes already use.
It is deliberately much smaller than "re-establish the lost PTY", which is not a
thing that can be done.

The two streams are not one stream
----------------------------------
The single easiest way to get this wrong. ``cursor`` is **not** an offset into
the host log:

- the host log holds what ``lmer`` printed *on the host* — its own chatter plus
  the attached container's output, teed together;
- ``/output`` reports the harness's PTY *inside* the container, counted by a
  bounded ring buffer with its own start and end offsets, which reports
  ``dropped_bytes`` when a cursor has already been evicted.

There is therefore no offset a re-attach can "resume from" in a shared space. It
starts at the offset the container reports *right now* (:func:`probe_health`
hands it over in the same request that proved the endpoint is up), and everything
before that is already in the log from before the restart. Starting at 0 instead
would replay the container's whole ring buffer underneath a copy of itself.

What the seam admits
--------------------
Full scrollback still holds (spec D16): the pre-restart bytes are on disk and are
not touched. But the two streams do not abut, and the seam marker says so rather
than joining them silently:

- host-side ``lmer`` chatter between the daemon's death and the re-attach is
  **gone**. It was written to a PTY nobody was draining and persisted nowhere.
  No amount of asking the container recovers it, because the container never had
  it.
- container output the ring buffer evicted before the platform got there is
  counted (``dropped_bytes``) and announced as a byte count in the log, because a
  silent gap in a terminal reads as a bug in the agent.

The session that needs none of this
-----------------------------------
Everything above describes a session whose only record is the host tee. A session
whose image writes its own log inside the container
(:func:`lmer_platform.spawn.container_log_path_for`, #150) never lost anything:
that file is written by a process the daemon's death does not touch, and
:func:`lmer_platform.session_io.canonical_log` serves it. Both of the things this
module would otherwise do to such a session are wrong, and wrong in the same way
— they act on a file nothing reads any more:

- the **drain** would append the container's output to the host tee for the life
  of the session, an HTTP long poll into the container every
  :data:`OUTPUT_POLL_SECONDS` producing bytes no reader will ever be served;
- the **seam marker** would announce a gap in a stream that has none. The record
  was written from inside, straight through the restart, so a line saying output
  is "recovered live from the control plane (from offset N)" is both untrue and
  planted in the wrong file.

So the log of record is resolved at re-attach time and the answer picks the
branch: :data:`OUTPUT_SESSION_LOG` and no drain, or the recovery above.

**At re-attach time** is the load-bearing half. ``canonical_log`` probes the
file's *content*, never a version — the writer ships in the session image, not in
this daemon — and the answer legitimately differs from what it was at spawn: a
session that detached before its supervisor wrote a byte has an empty
in-container log, resolves to ``host``, and needs the drain exactly like a session
from an older image. Nothing here caches the answer, on the entry or in this
process: a stale ``host`` keeps a pointless drain alive, and a stale ``container``
leaves a session whose own log never got a byte with no reader at all, which is
the original bug wearing a new hat.

A log that appears *after* a drain started keeps the drain
---------------------------------------------------------
The daemon can re-attach during a session's launch window — image pull, clone,
before the supervisor exists — decide ``host``, and start draining a session whose
own log begins filling a minute later. That drain is then feeding a file nothing
serves, and it is deliberately left running rather than stopped mid-flight
(:meth:`ControlDrain.poll_once` returns ``False`` to end the loop and would be the
place to hook it):

- it costs what it cost before #150 existed and nothing more, which is one long
  poll per :data:`OUTPUT_POLL_SECONDS`, and it is not a correctness problem: no
  reader is misled by bytes appended to a file no reader is served;
- **canonicity is not monotonic.** An in-container log that is emptied or
  unlinked — which its writer does on a failed write
  (``lmer_cli.supervisor.SessionLog.write``) — sends ``canonical_log`` back to the
  tee. A drain that is already running is then the only thing appending to the
  file that just became the record again, and stopping it on a signal that can
  flip back trades a poll nobody pays attention to for a session nobody can see;
- a stopped drain cannot be resumed where it left off. The cursor is not
  persisted (see the known limits below), so restarting one later means starting
  at the container's *current* offset and skipping whatever it produced in
  between.

For the same reason a re-attach of a session this daemon is already draining does
nothing but restate that fact. A running drain is a thread appending to the log
here and now, where the source probe and the health probe are both questions about
the container, so it outranks them: the entry can never say ``session_log`` while a
thread here is appending to the tee, and never ``none`` either. That second half is
the one a rescan meets. :func:`reattach_session`'s opening mark is provisional
("checking whether the container still answers") and every failure below it ends in
``none``, so a second pass over a healthy drained session used to leave "nothing is
reaching this log" on its entry with nothing to correct it until the drain gave up
— and the fleet view reads that as ``detached``
(:func:`lmer_platform.inventory._is_blind`), which is this module's own symptom
handed back by the code that fixes it. The drain-owned entry therefore says
``control_plane`` from the first line of the function, not from the point the probe
comes back.

Which mark stands when a rescan and a drain giving up land in the same instant is
settled by ``_ACTIVE``'s lock, not by timing. A drain deregisters and writes its
``none`` under that lock in one step, so a rescan either sees the drain and gets
its ``control_plane`` down first — where the ``none`` that follows immediately
corrects it — or finds no drain, which now means the final word is already
written, and asks the container itself. The order that used to be possible, a
rescan writing ``control_plane`` *after* a stopped drain's ``none``, left a blind
session labelled as watched until the next pass.

Detached is a state, not an adjective
-------------------------------------
A re-attached session is marked ``detached`` on its registry entry: the PTY is
gone and is never coming back, which is true whether or not the control plane
answered. The mark goes on **before** the probe, so a session whose control plane
is unreachable is left saying exactly what is known about it — "the process
exists, the platform cannot see it" — instead of showing as ``running`` on the
strength of a PID that nobody has been able to ask anything. Which is also why the
one session that gets a different opening mark is the one this daemon is already
draining (above): there the platform is *not* waiting on an answer to know what is
happening to the log.

The record distinguishes the outcomes with ``output``:

``control_plane``
    the drain is running and bytes are reaching the log again. The run stays
    ``running``, because it is.
``session_log``
    the session records itself inside its container and the platform serves that
    file, so there was nothing to recover and nothing to drain.
``none``
    nothing is appending to this log any more. The run reads ``detached``
    (:mod:`lmer_platform.inventory`), which is a distinct state from both
    ``running`` and ``crashed`` and is meant to be: the process is alive and the
    platform is blind to it.

Known limits, stated because they are easy to mistake for bugs
--------------------------------------------------------------
- **A second restart re-opens the same gap.** The drain in daemon #2 dies with
  it, and daemon #3 re-attaches at the container's *current* end offset again —
  so output produced during that second gap is skipped even though the ring
  buffer may still hold it. Fixing it means persisting the drain's advancing
  cursor, i.e. a registry write per poll; not paid here.
- **A fourth ``output`` value has to be taught to the fleet view separately.**
  :func:`lmer_platform.inventory._is_blind` enumerates the values that are *not*
  blind, so anything added here reads as ``detached`` in the fleet view — a live
  session labelled as one nobody can see — until that predicate learns it. Which
  is what ``session_log`` did: it was exactly this trap for as long as the
  predicate knew only ``control_plane``. The predicate lives in ``inventory``,
  which is not this module's file to change.
- **Nothing reaps a re-attached session's exit; a sweep reconciles it later.** The
  ``_watch`` thread that removes a cleanly-exited session's registry entry died
  with the old daemon too, so a re-attached session that finishes leaves a stale
  entry — and a stale entry on its own reads as ``crashed``
  (:mod:`lmer_platform.inventory`). The drain notices the process is gone (that is
  how it stops) but cannot tell a clean exit from a crash: it never had the exit
  code, and guessing is worse than the stale entry, which at least is evidence of
  *something*.

  What clears it is the detection tick's reconciliation sweep
  (:func:`lmer_platform.detect.sweep_finished_sessions`), which is not a guess
  either — it removes a stale entry only once the *run* has committed a terminal
  status, i.e. once the run's own record says it finished, and leaves every other
  dead entry alone. So a re-attached session that completes reads ``crashed`` until
  its last commit reaches the mirror and then stops being a crash at all: the run's
  row reads ``complete`` with no dead session hanging off it, and the entry no
  longer surfaces as a crashed row of its own once a later session holds the run key
  (:mod:`lmer_platform.detect` sets out both readings). One that really died keeps
  its entry and keeps reading ``crashed``. The exit code stays lost — nothing in
  either path ever had it — so the event the sweep appends says it is unknown
  instead of inventing a clean exit.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Optional

from . import registry
from .session_io import (
    ControlUnavailable,
    LOG_SOURCE_CONTAINER,
    SessionIOError,
    apply_resize,
    canonical_log,
    probe_health,
    read_control_output,
)
from .spawn import log_path_for
from .store import utc_now_iso

logger = logging.getLogger("lmer_platform.reattach")

__all__ = [
    "DETACH_REASON_DAEMON_RESTART", "OUTPUT_CONTROL_PLANE", "OUTPUT_NONE",
    "OUTPUT_SESSION_LOG",
    "OUTPUT_POLL_SECONDS", "FAILURE_BACKOFF_SECONDS", "MAX_CONSECUTIVE_FAILURES",
    "REATTACH_ROWS", "REATTACH_COLS",
    "ReattachReport", "ControlDrain",
    "detached_record", "mark_detached", "seam_marker", "dropped_marker",
    "reattach_session", "reattach_all", "active_drains", "startup_notice",
]

#: Why a session detached. One value today; named rather than inlined because the
#: UI shows it and a second reason (an operator detaching deliberately) would
#: otherwise arrive as a bare string nobody can grep for.
DETACH_REASON_DAEMON_RESTART = "daemon_restart"

#: ``detached.output``: the control-plane drain is running, so bytes are reaching
#: the session log again.
OUTPUT_CONTROL_PLANE = "control_plane"

#: ``detached.output``: the session writes its own log inside its container
#: (:func:`lmer_platform.spawn.container_log_path_for`) and the platform serves
#: that file, so the lost host PTY cost this session nothing — no gap, no drain,
#: no seam. Named as an outcome of a re-attach rather than left implicit because
#: it is the difference between "the platform recovered what it could" and "there
#: was nothing to recover", and an operator looking at a seam-free log after a
#: restart deserves to be told which.
OUTPUT_SESSION_LOG = "session_log"

#: ``detached.output``: nothing appends to this session's log any more. The
#: process may well be working; the platform has no way to see it.
OUTPUT_NONE = "none"

#: Seconds the supervisor holds an ``/output`` request open waiting for a byte.
#: A long poll rather than a fast one because there is no local file to stat
#: here: every tick is an HTTP round trip into the container, and the alternative
#: to blocking server-side is a request per idle tick per session forever. Twenty
#: sits under the route's own 30-second ceiling with room for the answer.
OUTPUT_POLL_SECONDS = 20.0

#: How long to wait after a failed poll before trying again.
FAILURE_BACKOFF_SECONDS = 1.0

#: Consecutive failed polls before the drain gives up on a session whose process
#: is still alive. Bounded rather than infinite: a control plane that has stopped
#: answering while the PID lingers is a container that died under a host process
#: that has not noticed yet, and a thread retrying that forever is a leak. Not
#: *one*, because a single blip must not cost an operator their only view of a
#: working session.
MAX_CONSECUTIVE_FAILURES = 5

#: Geometry pushed at a re-attached session that has never been told a size. The
#: classic default, and a floor rather than a guess at anyone's window: a browser
#: terminal posts its real size the moment it attaches. What this exists to fix is
#: ``0x0`` — a session spawned with no host terminal has nothing to seed its PTY,
#: and the operator's probe of the wedged session found exactly that, so a TUI in
#: there was rendering into a zero-by-zero screen.
REATTACH_ROWS = 24
REATTACH_COLS = 80

#: Drains this process is running, keyed by session id. The registry entry says a
#: session detached, but not whether *this* daemon already has a thread appending
#: to its log — and two drains on one log is the duplicated-output failure this
#: module's whole cursor discipline exists to avoid. Guarded because
#: :func:`reattach_all` is reachable from more than the startup path.
#:
#: The lock covers more than the dict: a drain's ``output`` mark and the entry in
#: here it speaks for are written under it together, so an entry's presence means
#: its drain has not yet had a final word and a rescan can trust what it reads
#: (:meth:`ControlDrain._give_up`, :func:`reattach_session`). It is therefore held
#: across one registry write on both sides, which is why nothing under it may ever
#: wait on a poll or an HTTP round trip.
_ACTIVE: dict = {}
_ACTIVE_LOCK = threading.Lock()


@dataclass(frozen=True)
class ReattachReport:
    """What re-attaching one session achieved. Returned, logged, and printed.

    ``draining`` says whether a thread in *this* process is appending to the host
    tee, and it is not the same question as "can the operator watch this session
    again": a session that records itself in-container is readable without a drain
    ever starting. ``output`` is the axis that answers *that* — it carries exactly
    what was written to the entry, so a caller (:func:`startup_notice`) does not
    have to re-derive from a boolean a distinction the entry already makes.
    ``detail`` is the sentence for whichever outcome it was.
    """

    session_id: str
    detached: bool
    draining: bool
    cursor: Optional[int] = None
    detail: str = ""
    output: str = OUTPUT_NONE


def detached_record(entry: Optional[dict]) -> Optional[dict]:
    """The ``detached`` record on a registry entry, or ``None``.

    A helper rather than ``entry.get("detached")`` at four call sites, because
    registry files are hand-editable: something that is not a mapping there must
    read as "not detached" rather than crashing the fleet view, which is the same
    tolerance :mod:`lmer_platform.registry` applies to a corrupt entry.
    """
    if not isinstance(entry, dict):
        return None
    record = entry.get("detached")
    return record if isinstance(record, dict) else None


def mark_detached(
    session_id: str,
    *,
    output: str,
    detail: str,
    cursor: Optional[int] = None,
    reason: str = DETACH_REASON_DAEMON_RESTART,
) -> Optional[dict]:
    """Record on the session's entry that its host PTY is gone.

    Written to the registry rather than held in memory because it has to survive
    the thing that caused it: the daemon that noticed may itself be restarted, and
    a session that quietly went back to reading ``running`` on the next boot is
    the failure this whole module exists to stop.

    Returns the updated entry, or ``None`` when there was nothing to update — a
    session that exited between the listing and this call. Not an error: the
    entry going away is the one outcome that needs no marking.

    A failed *write* is a different matter and propagates
    (:class:`~lmer_platform.store.StoreError`), like every other registry write.
    An unwritable state directory is not a condition to carry on quietly under,
    and the callers here are a startup path that reports per session and a drain
    thread that was ending anyway.
    """
    return registry.update(
        session_id,
        detached={
            "at": utc_now_iso(),
            "reason": reason,
            "output": output,
            "cursor": cursor,
            "detail": detail,
        },
    )


def seam_marker(cursor: int) -> bytes:
    """The one line written into the log where the two streams meet.

    One line, and honest about all three facts an operator needs at that point in
    the scrollback: where the break came from, that what follows is a *different*
    stream being appended to the same file, and that the host-side output during
    the gap is not missing-and-recoverable but gone. CR-LF because this lands in
    a stream a terminal emulator renders, where a bare LF leaves the next line
    indented by however far the cursor had got.
    """
    return (
        "\r\n── lmer platform: the daemon restarted and this session's host "
        "terminal was lost — output below is recovered live from the container's "
        f"control plane (from its offset {cursor}); anything lmer printed on the "
        "host during the gap was never recorded anywhere and cannot be "
        "recovered ──\r\n"
    ).encode("utf-8")


def dropped_marker(dropped: int) -> bytes:
    """The line written where the container's ring buffer evicted output.

    Announced as a byte count rather than smoothed over: a terminal that silently
    jumps looks like the agent lost its place, and an operator debugging that will
    go looking in the wrong process entirely.
    """
    return (
        f"\r\n── lmer platform: {dropped} bytes of this session's output were "
        "evicted from the container's buffer before the platform could read them "
        "and are lost ──\r\n"
    ).encode("utf-8")


def _append(session_id: str, data: bytes) -> bool:
    """Append raw bytes to a session's log. ``False`` when the log is unwritable.

    Opened per call rather than held for the drain's life: the appends are
    seconds apart at worst, ``O_APPEND`` makes the position correct regardless of
    who else is writing, and a long-lived handle on a log the operator may rotate
    or delete is a fd pinning an unlinked inode for the life of the session.
    """
    path = log_path_for(session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab", buffering=0) as sink:
            sink.write(data)
    except OSError as exc:
        logger.warning(
            "platform_reattach_log_write_failed id=%s path=%s error=%s",
            session_id, path, exc,
        )
        return False
    return True


class ControlDrain:
    """Polls one session's ``/output`` into its log until the session is gone.

    The successor to :func:`lmer_platform.spawn._drain`, and deliberately not a
    reimplementation of it: that one owns an fd and must never stop while the
    child lives (an undrained PTY blocks the writer). This one owns nothing. The
    container's buffer is bounded and evicts on its own, so giving up here costs
    visibility, never the session.

    Termination is a property of the session, not of a flag: each pass
    re-resolves the control endpoint, which checks liveness first, so the loop
    ends by itself when the process disappears. :meth:`stop` exists for a daemon
    shutting down cleanly, not as the normal way out.
    """

    def __init__(
        self,
        session_id: str,
        *,
        cursor: int,
        poll: float = OUTPUT_POLL_SECONDS,
        backoff: float = FAILURE_BACKOFF_SECONDS,
    ) -> None:
        self.session_id = session_id
        self.cursor = int(cursor)
        self.poll = poll
        self.backoff = backoff
        self.failures = 0
        self._stop = threading.Event()

    def stop(self) -> None:
        """Ask the loop to finish after its current poll."""
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def start(self) -> threading.Thread:
        """Run the loop on a daemon thread, registering it as this session's."""
        with _ACTIVE_LOCK:
            if self.session_id in _ACTIVE:
                raise RuntimeError(
                    f"a control-plane drain is already running for "
                    f"{self.session_id} — a second one would append the same "
                    "bytes to the log twice"
                )
            _ACTIVE[self.session_id] = self
        thread = threading.Thread(
            target=self.run,
            name=f"lmer-platform-control-drain-{self.session_id}",
            daemon=True,
        )
        thread.start()
        return thread

    def run(self) -> None:
        """The loop. Returns when the session ends or the drain gives up."""
        try:
            while not self._stop.is_set() and self.poll_once():
                pass
        finally:
            # The exit for a drain that ended without a final mark — the session
            # over, or :meth:`stop` on a daemon shutting down. One that gave up
            # deregistered itself with its mark and is already gone from here,
            # which the identity check covers.
            with _ACTIVE_LOCK:
                if _ACTIVE.get(self.session_id) is self:
                    del _ACTIVE[self.session_id]

    def poll_once(self) -> bool:
        """One read-and-append pass. ``False`` means this drain is finished.

        Split out of :meth:`run` because every interesting decision the drain
        makes lives in one pass — where the cursor comes from, what a dropped
        count does, when a failure stops being transient — and a loop is a poor
        place to observe any of them.
        """
        try:
            chunk = read_control_output(
                self.session_id, cursor=self.cursor, timeout=self.poll
            )
        except ControlUnavailable as exc:
            # The endpoint resolver checks liveness before anything else, so this
            # is the session having ended — the ordinary way out, not a failure
            # worth a warning.
            logger.info(
                "platform_reattach_drain_ended id=%s (%s)", self.session_id, exc
            )
            return False
        except SessionIOError as exc:
            self.failures += 1
            if self.failures >= MAX_CONSECUTIVE_FAILURES:
                self._give_up(str(exc))
                return False
            logger.debug(
                "platform_reattach_poll_failed id=%s attempt=%d error=%s",
                self.session_id, self.failures, exc,
            )
            self._stop.wait(self.backoff)
            return True

        # Reset after a pass that actually read something back: the cap counts
        # *consecutive* failures, so a blip in a session that runs for hours does
        # not accumulate toward a give-up it never deserved.
        self.failures = 0
        if chunk.dropped:
            logger.warning(
                "platform_reattach_output_dropped id=%s bytes=%d",
                self.session_id, chunk.dropped,
            )
            if not _append(self.session_id, dropped_marker(chunk.dropped)):
                self._give_up("the session log became unwritable")
                return False
        if chunk.data and not _append(self.session_id, chunk.data):
            self._give_up("the session log became unwritable")
            return False
        # From the answer, never cursor + len(data): the route hands back decoded
        # text whose length is not the byte count the ring buffer advanced by
        # (see session_io.ControlOutput).
        self.cursor = chunk.cursor
        return True

    def _give_up(self, why: str) -> None:
        """Stop draining, and make the entry say the log went quiet again.

        The mark is the point. A drain that died leaving ``output:
        control_plane`` on the entry claims a live view the platform no longer
        has, which is the same lie the untreated daemon restart told.

        Deregistering here — first, and in the same critical section as the mark
        — is what makes membership of ``_ACTIVE`` mean "this drain has not yet had
        its final word", which is the fact :func:`reattach_session` reads it for.
        Marking first and leaving the removal to :meth:`run`'s ``finally`` left a
        window in which a rescan saw a drain that had already written ``none`` and
        wrote ``control_plane`` over it, labelling a blind session watched until
        the next pass. Both orders around this lock are safe: a rescan that gets
        in first marks ``control_plane`` and this mark then lands after it and
        wins, and one that arrives after finds no drain and asks the container
        itself.
        """
        logger.warning(
            "platform_reattach_drain_gave_up id=%s reason=%s", self.session_id, why
        )
        with _ACTIVE_LOCK:
            if _ACTIVE.get(self.session_id) is self:
                del _ACTIVE[self.session_id]
            mark_detached(
                self.session_id,
                output=OUTPUT_NONE,
                detail=(
                    "the platform was reading this session over its control plane "
                    f"and stopped being able to: {why}. The process may still be "
                    "working; nothing is being recorded."
                ),
                cursor=self.cursor,
            )


def active_drains() -> list:
    """Session ids this process is currently draining. For tests and diagnostics."""
    with _ACTIVE_LOCK:
        return sorted(_ACTIVE)


def _resize_if_unsized(session_id: str, health: dict) -> None:
    """Push a geometry at a PTY that has never been told one. Best-effort.

    Only when the reported size has a zero dimension or is missing. Re-applying a
    default over a size the session already has would *shrink* a terminal that
    was correctly sized, and the platform has no idea what the operator's window
    is — the browser terminal posts that itself on attach.

    Quiet, like every other resize (see :func:`session_io.apply_resize`): a
    session rendering at the wrong width is cosmetic, and a re-attach that
    aborted over it would cost the operator the output recovery it came for.
    """
    rows, cols = health.get("rows"), health.get("cols")
    if isinstance(rows, int) and isinstance(cols, int) and rows > 0 and cols > 0:
        return
    try:
        report = apply_resize(session_id, REATTACH_ROWS, REATTACH_COLS)
    except SessionIOError as exc:
        logger.warning(
            "platform_reattach_resize_failed id=%s error=%s", session_id, exc
        )
        return
    logger.info(
        "platform_reattach_resized id=%s applied=%s was=%sx%s",
        session_id, report.applied, rows, cols,
    )


def _records_itself(session_id: str) -> bool:
    """Whether the log of record for *session_id* is the one it writes itself.

    A one-line wrapper over :func:`session_io.canonical_log` and worth its own
    name for what it does *not* do: it holds nothing. Every call re-probes,
    because the answer is a fact about a file's content right now — see the module
    docstring on why a cached answer is wrong in both directions.

    The probe, not a version check, is also why this asks ``session_io`` instead of
    deciding for itself. Two notions of "which log is canonical" is how the drain
    ends up filling a file the read path stopped serving, which is the whole
    finding this branch exists to fix.
    """
    _, source = canonical_log(session_id)
    return source == LOG_SOURCE_CONTAINER


def reattach_session(
    session_id: str, *, poll: float = OUTPUT_POLL_SECONDS
) -> ReattachReport:
    """Mark one session detached and, if it answers, start reading it again.

    The order is the design and is not an implementation detail:

    0. **A drain this process owns short-circuits everything.** It is the one thing
       known before anything is asked, it outranks every answer that could come
       back, and it makes the whole rest of the function a no-op: the session is
       detached, its log is being appended to, and the entry is re-marked to say so
       (module docstring, "A log that appears *after* a drain started keeps the
       drain").
    1. **Mark detached first.** Whatever happens next, the host PTY is gone. A
       failure later must not leave the session reading as ``running``.
    2. **Probe.** ``/healthz`` is the only thing that distinguishes a session the
       platform lost sight of from a session that is over, and it is asked once.
       No answer means no answer: the session stays detached and this returns.
       Retrying until something says yes is how a dead session gets reported as
       live.
    3. **Ask which log is the record.** A session that writes its own inside the
       container has nothing to recover, and both the seam and the drain would act
       on a file nobody is served from (see the module docstring). Asked here
       rather than remembered from the spawn, and after the probe rather than
       before it, so a session whose container has stopped answering is still
       reported blind instead of credited with a live self-record.
    4. **Seam, then drain,** starting at the offset the probe reported — the
       current end, not zero, or the container's whole buffer lands in the log
       underneath the copy the PTY tee already wrote.
    5. **Resize,** but only if the probe says nothing ever sized this PTY. Both
       branches do it: a 0x0 PTY is a fact about the container, not about which
       file its output lands in.

    Returns a :class:`ReattachReport`. Never raises for a session that is simply
    unreachable — a daemon that refused to start because one container was gone
    would be a worse failure than the one this fixes.
    """
    # Read before the entry is written, because it decides what is true to write:
    # a drain registered here is a thread appending to this session's log now, and
    # the mark below is otherwise a placeholder for an answer that has not come
    # back. Nothing on the ordinary path would ever replace that placeholder for
    # such a session — no drain starts, and every failure branch ends in
    # ``OUTPUT_NONE`` — so it would stand as "nothing is reaching this log" over a
    # session being read fine until the drain gave up.
    #
    # The read and the mark it decides are one critical section, because a drain
    # gives up under this same lock (:meth:`ControlDrain._give_up`): a lookup that
    # sees a drain must get its ``control_plane`` mark down before that drain can
    # write ``none`` and go, or this writes a live view over the entry of a session
    # nothing is reading any more. Only the mark is in here — the probe below is an
    # HTTP round trip and must not be.
    already_detail = "already being read over its control plane by this daemon"
    with _ACTIVE_LOCK:
        drain = _ACTIVE.get(session_id)
        if drain is None:
            marked = mark_detached(
                session_id,
                output=OUTPUT_NONE,
                detail=(
                    "the daemon restarted, so this session's host terminal is "
                    "gone; checking whether the container still answers"
                ),
            )
        else:
            marked = mark_detached(
                session_id,
                output=OUTPUT_CONTROL_PLANE,
                detail=already_detail,
                # The drain's own position rather than a probe's: it is where this
                # session's output is being read from.
                cursor=drain.cursor,
            )
    if marked is None:
        return ReattachReport(
            session_id=session_id,
            detached=False,
            draining=False,
            detail="the session's registry entry vanished before it could be marked",
        )

    if drain is not None:
        # Everything below is about establishing a reader for this session, which
        # it has. Returning here is also what keeps the two probes off it: a health
        # blip must not be able to unmark a working drain, and which log is
        # canonical cannot be allowed to relabel one (T78).
        return ReattachReport(
            session_id=session_id,
            detached=True,
            draining=True,
            cursor=drain.cursor,
            detail=already_detail,
            output=OUTPUT_CONTROL_PLANE,
        )

    try:
        health = probe_health(session_id)
    except SessionIOError as exc:
        detail = (
            f"its control plane did not answer ({exc}). The process is still "
            "there; nothing can be read from it, and nothing is being recorded."
        )
        mark_detached(session_id, output=OUTPUT_NONE, detail=detail)
        logger.warning(
            "platform_reattach_unreachable id=%s error=%s", session_id, exc
        )
        return ReattachReport(
            session_id=session_id, detached=True, draining=False, detail=detail
        )

    # Only ever reached with no drain of ours running, which is what makes the
    # answer safe to act on: a log that appeared under a running drain must leave
    # both the drain and the entry alone (module docstring, "A log that appears
    # after a drain started keeps the drain"), and that case returned above.
    if _records_itself(session_id):
        detail = (
            "its host terminal is gone for good, but this session writes its own "
            "log inside its container and the platform serves that, so there was "
            "nothing to recover: the record has no gap and needs nothing appended "
            "to it from the host"
        )
        mark_detached(session_id, output=OUTPUT_SESSION_LOG, detail=detail)
        _resize_if_unsized(session_id, health)
        logger.info("platform_reattach_self_recorded id=%s", session_id)
        return ReattachReport(
            session_id=session_id,
            detached=True,
            draining=False,
            detail=detail,
            output=OUTPUT_SESSION_LOG,
        )

    cursor = health.get("cursor")
    if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
        # Without a starting offset there is no safe place to begin: zero would
        # replay the ring buffer under the scrollback that already holds it, and
        # inventing one would skip output nobody can get back. Refusing to drain
        # is the only answer that cannot corrupt the log.
        detail = (
            "its control plane answered a health probe without a usable output "
            f"cursor ({cursor!r}), so there is no offset to resume reading from"
        )
        mark_detached(session_id, output=OUTPUT_NONE, detail=detail)
        logger.warning("platform_reattach_no_cursor id=%s cursor=%r", session_id, cursor)
        return ReattachReport(
            session_id=session_id, detached=True, draining=False, detail=detail
        )

    if not _append(session_id, seam_marker(cursor)):
        detail = (
            "its control plane answered, but this session's log cannot be "
            "written to, so there is nowhere to put the recovered output"
        )
        mark_detached(session_id, output=OUTPUT_NONE, detail=detail, cursor=cursor)
        return ReattachReport(
            session_id=session_id, detached=True, draining=False, detail=detail
        )

    detail = (
        "its host terminal is gone for good, but the container answered and its "
        "output is being recovered over the control plane; host-side lmer output "
        "during the restart is not recoverable"
    )
    mark_detached(
        session_id, output=OUTPUT_CONTROL_PLANE, detail=detail, cursor=cursor
    )
    _resize_if_unsized(session_id, health)
    ControlDrain(session_id, cursor=cursor, poll=poll).start()
    logger.info(
        "platform_reattach_draining id=%s cursor=%d", session_id, cursor
    )
    return ReattachReport(
        session_id=session_id,
        detached=True,
        draining=True,
        cursor=cursor,
        detail=detail,
        output=OUTPUT_CONTROL_PLANE,
    )


def reattach_all(*, poll: float = OUTPUT_POLL_SECONDS) -> list:
    """Re-attach every session that survived the daemon. Returns one report each.

    Scoped to :func:`registry.list_sessions` with ``live_only=True``, which is
    the registry's own liveness rule — ``kill(pid, 0)`` plus the ``/proc`` zombie
    check that exists because the daemon is these sessions' parent and an
    unreaped child answers a bare existence probe. Reused rather than restated:
    two notions of "alive" is how a dead session ends up with a thread polling a
    port some unrelated process now owns.

    A stale entry is skipped entirely. It is the crash signal the inventory reads
    (:mod:`lmer_platform.inventory`), and marking a corpse ``detached`` would
    overwrite evidence of a death with a claim about a terminal.
    """
    reports = []
    for entry in registry.list_sessions(live_only=True):
        session_id = entry.get("id")
        if not isinstance(session_id, str):
            continue
        try:
            reports.append(reattach_session(session_id, poll=poll))
        except Exception as exc:
            # One unrecoverable session must not stop the daemon from starting,
            # or from re-attaching the rest of the fleet.
            logger.warning(
                "platform_reattach_failed id=%s error=%r", session_id, exc
            )
    return reports


def startup_notice(reports: list) -> Optional[str]:
    """One operator-facing line for what a restart cost, or ``None`` if nothing.

    Printed at daemon start beside the other notices. Worth a line because the
    symptom it explains — a terminal with a seam in it — is otherwise
    indistinguishable from the session having gone strange, and because a session
    left undrained is a thing the operator can act on (open it, or kill it) only
    if they are told.

    Counted over sessions that were actually marked, not over reports: a session
    whose entry vanished mid-re-attach produces a report and no survivor, and a
    notice announcing a survivor there would point at nothing.

    Bucketed three ways, because the seam is no longer a property of surviving: a
    session that records itself in-container came through with an unbroken log, and
    telling an operator to expect a seam in it would send them looking for a break
    that is not there.
    """
    survivors = [r for r in reports if r.detached]
    if not survivors:
        return None
    recorded = [r for r in survivors if r.output == OUTPUT_SESSION_LOG]
    draining = [r for r in survivors if r.draining]
    blind = [
        r for r in survivors
        if not r.draining and r.output != OUTPUT_SESSION_LOG
    ]
    parts = [
        f"🔌 {len(survivors)} session(s) survived the last daemon: their host "
        "terminals did not."
    ]
    if recorded:
        parts.append(
            f"   {len(recorded)} kept their own in-container log, so nothing was "
            "lost: " + ", ".join(r.session_id for r in recorded)
        )
    if draining:
        parts.append(
            f"   {len(draining)} being read over the control plane, with a seam in "
            "the log where the platform re-attached: "
            + ", ".join(r.session_id for r in draining)
        )
    if blind:
        parts.append(
            f"   {len(blind)} unreachable — alive, but nothing is recorded: "
            + ", ".join(r.session_id for r in blind)
        )
    return "\n".join(parts)
