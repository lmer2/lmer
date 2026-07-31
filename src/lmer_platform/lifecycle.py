"""Ending a session on purpose: wind down and exit (spec §7.5 / D22, T27).

Sessions end **only** because a human asked (D22). There is no idle timeout, no
reaper, and nothing on a timer in this module — the property that buys is the one
the orchestrator is for: *a session that bound a port and set something up for you
to look at must never be shut down out from under you.* Two verbs, and the whole
design is in the difference between them:

``wind down``
    A **request**, delivered as a prompt over the session's own control plane.
    The agent commits, pushes, records run state, posts its summary, and then ends
    the session itself. The platform signals nothing and waits.

``exit``
    The platform **signals**, now. The agent gets nothing — no commit, no push, no
    summary. Everything uncommitted inside that container is gone.

They are not two spellings of "stop", so nothing here lets one degrade into the
other: a wind-down never escalates to a signal, and an exit never pretends to have
asked first. The UI reflects that with deliberately unequal prominence (§10.2), and
the *reason* an operator can be given a blunt verb at all is that it is never
reached by accident.

Why wind down is input and not a new transport
----------------------------------------------
Because it is a sentence addressed to an agent, and the platform already has
exactly one way to say a sentence to a session: ``POST /input`` on its control
plane (:func:`lmer_platform.session_io.send_input`). That path is an HTTP call to a
process *inside the container*, which is what makes it the only half of the
platform that survives a daemon restart — see :mod:`lmer_platform.reattach`. So
wind down works on a re-attached session, and exit does not; that asymmetry is not
an accident of implementation, it is the reason wind down is the default verb.

The prompt is one paragraph with no newlines in it, deliberately. It lands in a
PTY in raw mode, where the control plane turns "and press Enter" into a CR
(``supervisor._ensure_submit_cr``) — but a bare LF *inside* the payload is at the
mercy of whatever TUI is reading: claude's inserts it as a literal newline in the
input box, and a harness that instead submits on the first one would deliver the
agent a truncated instruction ("you have been asked to wind down" and nothing about
what that means). One paragraph cannot be truncated that way. An operator note is
whitespace-collapsed on the way in for the same reason, since a note typed into a
textarea is the obvious source of a newline.

Why exit signals the process **group**
--------------------------------------
``lmer`` is not the thing holding the container. It runs ``podman run`` as its own
child and installs no signal handler of its own
(``lmer_cli.cli``), so a SIGTERM delivered to the session's pid alone kills the
bookkeeping and leaves a container running with nothing watching it. The session is
spawned with ``start_new_session=True``
(:func:`lmer_platform.spawn.spawn_session`), so its pid *is* a process-group id, and
signalling the group reaches ``podman run`` too — which is what actually stops and
removes the container. Group leadership is checked rather than assumed, because
``killpg`` on a pid that does not lead one signals whatever group happens to carry
that id. Same discipline as ``lmer_cli.container.spawn_harness``'s group kill and
:func:`lmer_platform.assistant.stop`.

The ladder is SIGTERM, then SIGKILL, and the first step is the one that matters:
SIGTERM lets ``podman run`` tear the container down, while SIGKILL only guarantees
that the *session* ends and can leave the container behind (the lingering-grandchild
case :func:`lmer_platform.spawn._scrub_transcripts` describes). So SIGTERM gets a
generous grace and SIGKILL is the backstop.

Which sessions may be signalled at all
--------------------------------------
Only a session **this process is the parent of**. Not a policy — a safety property.
The daemon holds an un-reaped child slot for every session it spawned, so that pid
cannot be recycled underneath it; for any other session the pid on the entry is a
number that was true once, and a process that exited an hour ago may have had its
pid reused by something entirely unrelated. Signalling *that* is not a failed exit,
it is killing a stranger's process. Two sessions are refused on exactly those
grounds:

- one that survived a daemon restart (:mod:`lmer_platform.reattach` marked it
  ``detached``): the daemon re-adopted its *log*, never its child slot;
- one spawned by a process that has gone — ``lmer platform spawn`` spawns and
  exits, so its sessions are orphans from birth.

Both are told to wind down instead, which still works, and are given the pid so the
operator can finish the job by hand if they mean to. Parentage is read from the
kernel (``/proc/<pid>/stat``) rather than from the entry's ``owner_pid``, because a
registry entry is a hand-editable debugging artifact and this decision ends in a
signal. Where ``/proc`` is absent — macOS — the entry's own claim is the fallback,
which degrades to trusting a file we would rather verify; the alternative is a verb
that does not exist on that platform at all.

What gets recorded, and where it may not be recorded
----------------------------------------------------
In the platform's own state, and nowhere else. A wind-down leaves a ``lifecycle``
record on the session's registry entry (which is how the UI can say "asked four
minutes ago, still going") and a line in the platform's event log; an exit leaves
the events. **Run state is never written** (spec D3): the run's own record of what
happened is the agent's to write, which is exactly the thing wind down exists to
give it the chance to do, and an exit that scribbled "terminated" into the mirror
would be the platform asserting an ending the run never got to describe.

The backstop is a recorded deadline, not a timer
------------------------------------------------
An agent that never finishes wrapping up would otherwise hold its slot forever
(spec R18), so a wind-down records ``backstop_at``. Nothing in this module acts on
it: past that point the UI says the wind-down has not completed and offers exit,
and the operator decides. D22 forbids the obvious shortcut — auto-escalating to a
signal — and surfacing beats guessing.

A note on the twin in :mod:`lmer_platform.assistant`
----------------------------------------------------
``assistant.stop`` carries its own copy of this ladder and says in its own docstring
that it should collapse into this slice rather than grow a twin. It is left alone
here because it does more than terminate: it also clears the assistant pointer,
records ``stopped_at``/``stop_reason``, and distinguishes an operator stop from a
rotation. Until that merge happens :func:`exit_session` refuses an ``assistant``
session outright rather than silently taking the bookkeeping away from it.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import registry
from .assistant import KIND as ASSISTANT_KIND
from .reattach import detached_record
from .session_io import send_input
from .store import StoreError, append_event, utc_now_iso

logger = logging.getLogger("lmer_platform.lifecycle")

__all__ = [
    "LifecycleError", "SessionNotTerminable", "TerminationFailed",
    "WindDownReport", "ExitReport",
    "VERB_WIND_DOWN", "VERB_EXIT", "WIND_DOWN_PROMPT", "PLATFORM_PREFIX",
    "MAX_NOTE_CHARS", "WIND_DOWN_BACKSTOP_SECONDS",
    "EXIT_GRACE_SECONDS", "EXIT_KILL_GRACE_SECONDS",
    "wind_down_prompt", "wind_down", "exit_session",
]

#: The two verbs, as recorded on an entry and in the event log. Named because they
#: end up in state a later slice reads back; a bare string in two files is how the
#: UI ends up matching on a value the daemon stopped writing.
VERB_WIND_DOWN = "wind_down"
VERB_EXIT = "exit"

#: Marks input the platform typed rather than the operator. The scrollback of a
#: session that was asked to wind down is read later by someone trying to work out
#: why it stopped, and "who said this" is the first question. ASCII on purpose: this
#: is written into a PTY belonging to a harness whose input handling is not ours.
PLATFORM_PREFIX = "[lmer platform]"

#: What a wind-down actually asks for. One paragraph, no newlines — see the module
#: docstring for why that is a correctness property and not formatting.
#:
#: It says "not a new task" first because that is the failure mode: an agent mid-turn
#: reads any incoming text as work, and a wind-down that starts a fresh
#: investigation is worse than no wind-down. It ends on the unreviewed-work case,
#: which is D22's whole reason for existing: a session may be holding a port or a
#: page nobody has looked at, and the operator asking it to stop is not the same as
#: the operator having seen it.
#:
#: It names ``lmer-end-session`` outright. The first version deliberately did not,
#: on the reasoning that a prompt naming a command would need editing when the
#: command arrived — but that left the agent asked to end its session with no
#: generic way to do it (only Slack-coupled ``lmer-slack end-session`` existed, and
#: only the ``chat`` taskdef documented it). A wind-down whose instruction an agent
#: cannot act on is a button whose effect depends on guessing, so the command is
#: named and the fallback clause stays for a taskdef that ends sessions its own way.
WIND_DOWN_PROMPT = (
    f"{PLATFORM_PREFIX} The operator has asked you to wind this session down. "
    "This is not a new task: stop picking up work, and bring what you already have "
    "to a landed state — commit and push it the way your instructions require, "
    "record the run's state, and post the summary or report your task expects. "
    "Then end the session by running lmer-end-session — or, if that command is "
    "not on your PATH, python -m lmer_cli.session_end, which is the same thing "
    "and works in an image built before the console script existed. Either is "
    "fine; use whatever your instructions say instead if they specify something. "
    "Nothing is going to kill this container out from under you, so take the time "
    "to finish properly. If something is genuinely unfinished, or if you are "
    "holding something the operator has not seen yet — a port you bound, a page you "
    "asked them to look at — say so in your summary before you go."
)

#: Ceiling on an operator's addition to the prompt. It is typed into a browser and
#: ends up in a registry entry and a PTY, so it is bounded here rather than trusted
#: to be short. Matches :data:`lmer_platform.assistant.MAX_NOTE_CHARS`.
MAX_NOTE_CHARS = 2000

#: How long a wind-down is given before the UI starts saying it has not finished
#: (spec R18). Generous, because "wrapping up" legitimately means a test run, a
#: push and a review comment. Nothing escalates when it passes — see the module
#: docstring.
WIND_DOWN_BACKSTOP_SECONDS = 30 * 60.0

#: How long an exit waits for SIGTERM to work before escalating, and how long it
#: then waits for SIGKILL. The first is generous because that is the signal that
#: gets the *container* removed; the second is short because nothing survives it.
#: Same values as :mod:`lmer_platform.assistant`'s stop.
EXIT_GRACE_SECONDS = 5.0
EXIT_KILL_GRACE_SECONDS = 2.0

#: How often the wait polls for the process to be gone.
_EXIT_POLL_SECONDS = 0.05


class LifecycleError(RuntimeError):
    """Base refusal, carrying the status a route should answer.

    The status rides on the exception exactly as in
    :mod:`lmer_platform.session_io` and :mod:`lmer_platform.assistant`: the routes
    get one handler, and a refusal added later arrives with its own code instead of
    falling through to a 500 with a traceback.
    """

    status = 500


class SessionNotTerminable(LifecycleError):
    """This session cannot be signalled, and the message says what to do instead.

    409 rather than 400: the request was well-formed, and the reason is a fact
    about the session rather than a mistake by the caller. The distinction matters
    to the client, because the answer is always the same — wind it down instead.
    """

    status = 409


class TerminationFailed(LifecycleError):
    """It was signalled, all the way to SIGKILL, and is still there.

    A 500: the platform was asked to do something it is supposed to be able to do
    and could not. The session's registry entry is deliberately left alone, so the
    fleet view keeps showing a session that is still running.
    """


@dataclass(frozen=True)
class WindDownReport:
    """What a wind-down request achieved. Returned, logged, and sent to the client.

    ``recorded`` is the one field that can be false on a successful request: the
    prompt is delivered first and marked second, so a session that exited in
    between (or a state dir that went unwritable) loses the mark and not the
    request. Reporting it rather than raising keeps the operator from being told
    their wind-down failed when the agent has already been asked.
    """

    session_id: str
    requested_at: str
    backstop_at: str
    prompt: str
    recorded: bool

    def to_dict(self) -> dict:
        return {
            "session": self.session_id,
            "verb": VERB_WIND_DOWN,
            "requested_at": self.requested_at,
            "backstop_at": self.backstop_at,
            "prompt": self.prompt,
            "recorded": self.recorded,
        }


@dataclass(frozen=True)
class ExitReport:
    """What an exit did. Only ever returned when the session is actually gone.

    ``signals`` is the ladder as far as it was walked, so an exit that needed
    SIGKILL is distinguishable afterwards from one that went quietly — the first is
    a session that ignored SIGTERM, which is worth knowing when its container turns
    out to still be running.
    """

    session_id: str
    pid: int
    at: str
    signals: tuple
    entry_removed: bool

    def to_dict(self) -> dict:
        return {
            "session": self.session_id,
            "verb": VERB_EXIT,
            "pid": self.pid,
            "at": self.at,
            "signals": list(self.signals),
            "entry_removed": self.entry_removed,
        }


def _iso_after(seconds: float) -> str:
    """A timestamp *seconds* from now, in the format both state layers use."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _validated_note(note: Optional[str]) -> Optional[str]:
    """The operator's addition to the prompt, or ``None``.

    Whitespace is collapsed rather than preserved: this text is appended to a
    payload that must not contain a newline (see the module docstring), and a note
    typed into a textarea is where one comes from. Length is checked *after*
    collapsing, so a note that is only long because it was pasted with hard wraps
    is not refused for it.
    """
    if note is None:
        return None
    if not isinstance(note, str):
        raise LifecycleError(
            f"a wind-down note must be text, got {type(note).__name__}"
        )
    collapsed = " ".join(note.split())
    if not collapsed:
        return None
    if len(collapsed) > MAX_NOTE_CHARS:
        raise LifecycleError(
            f"wind-down note is {len(collapsed)} characters; the limit is "
            f"{MAX_NOTE_CHARS}"
        )
    return collapsed


def wind_down_prompt(note: Optional[str] = None) -> str:
    """The exact text a wind-down sends, so a caller can show it before sending.

    Public because the route returns it and the UI shows it: an operator about to
    interrupt a working agent should be able to read what it will be told, and a
    prompt that only exists inside the call that sends it cannot be reviewed.
    """
    validated = _validated_note(note)
    if not validated:
        return WIND_DOWN_PROMPT
    return f"{WIND_DOWN_PROMPT} The operator added: {validated}"


def wind_down(session_id: str, *, note: Optional[str] = None) -> WindDownReport:
    """Ask a session's agent to wrap up and end itself. Signals nothing.

    Loud on delivery, quiet on bookkeeping, and that split is the point. Getting the
    prompt into the session is the whole operation, so every way that can fail
    raises (:class:`lmer_platform.session_io.SessionIOError` and its subclasses
    carry the status): an operator who is told their session was asked to wind down
    when it never heard anything will come back hours later to a session that is
    still working. Failing to *record* the request afterwards is reported in the
    return value instead — the agent has already been asked, and turning that into
    an error would be a lie in the other direction.

    Works on a re-attached session, which is the case that decides the shape of the
    whole module: the prompt is an HTTP call into the container, so the death of a
    daemon's host PTY does not touch it (:mod:`lmer_platform.reattach`).
    """
    prompt = wind_down_prompt(note)
    # Before the mark, so nothing claims a session was asked to wind down until it
    # has been. append_newline is the control plane's "and press Enter", without
    # which the paragraph sits unsent in the agent's input box.
    send_input(session_id, prompt, append_newline=True)

    requested_at = utc_now_iso()
    backstop_at = _iso_after(WIND_DOWN_BACKSTOP_SECONDS)
    record = {
        "verb": VERB_WIND_DOWN,
        "requested_at": requested_at,
        "backstop_at": backstop_at,
        # The note, not the whole prompt: the fixed part is a constant in this
        # module, and copying it into every entry would make a prompt change look
        # like a state migration.
        "note": _validated_note(note),
        "detail": (
            "asked to wrap up and end itself; the platform is not going to signal "
            "it, so it ends when the agent decides it is done"
        ),
    }
    recorded = False
    try:
        recorded = registry.update(session_id, lifecycle=record) is not None
    except StoreError as exc:
        # The prompt is already delivered. Loud in the log, because what is lost is
        # the UI's ability to say this session was asked — but not fatal to a
        # request that has, in the only sense that matters, succeeded.
        logger.error(
            "platform_wind_down_unrecorded id=%s error=%s — the session was asked "
            "to wind down but the platform could not record it", session_id, exc,
        )
    append_event(
        "session_wind_down_requested",
        note=session_id,
        data={
            "session": session_id,
            "recorded": recorded,
            # Whether the operator added anything, not what: the note itself is on
            # the entry, and the event log is the thing people paste around.
            "noted": bool(record["note"]),
        },
    )
    logger.info(
        "platform_session_wind_down id=%s recorded=%s", session_id, recorded
    )
    return WindDownReport(
        session_id=session_id,
        requested_at=requested_at,
        backstop_at=backstop_at,
        prompt=prompt,
        recorded=recorded,
    )


def _parent_pid(pid: int) -> Optional[int]:
    """*pid*'s parent according to the kernel, or ``None`` where that is unknowable.

    Reads ``/proc/<pid>/stat``, whose field after the process state is the ppid. The
    ``comm`` field is parenthesised and may itself contain spaces, so everything is
    taken from after the final ``)`` — the same parse
    :func:`lmer_platform.registry._is_zombie` makes, for the same reason.

    ``None`` means "this host cannot say" (no ``/proc``, as on macOS), never "no
    parent": the caller falls back to the registry's own claim rather than treating
    an unreadable file as permission.
    """
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as handle:
            stat_line = handle.read()
    except OSError:
        return None
    _, _, rest = stat_line.rpartition(")")
    fields = rest.split()
    if len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def _signallable_pid(session_id: str, entry: dict) -> int:
    """The pid an exit may signal for this session, or a refusal saying why not.

    Every branch here is a way of not signalling a process that is not ours to
    signal; see the module docstring on why that is the property being defended
    rather than a policy about who owns what.
    """
    pid = entry.get("pid")
    # ``bool`` is an ``int`` subclass, and 0 and -1 mean "every process I can
    # signal" to kill(2). Reachable, not theoretical: the pid is read back out of a
    # file an operator can edit.
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        raise SessionNotTerminable(
            f"session {session_id}'s registry entry carries no usable pid "
            f"({pid!r}), so there is nothing this platform can signal"
        )
    if pid == os.getpid():
        raise SessionNotTerminable(
            f"session {session_id}'s entry names this daemon's own pid ({pid}) — "
            "refusing to signal it, since that would take the platform down with "
            "the session"
        )

    if entry.get("kind") == ASSISTANT_KIND:
        raise SessionNotTerminable(
            f"session {session_id} is the orchestrating assistant, not a worker. "
            "Stopping it also has to clear the pointer and the stop reason in the "
            "assistant's own state, which this verb does not own — use the "
            "assistant's stop instead"
        )

    detached = detached_record(entry)
    if detached is not None:
        raise SessionNotTerminable(
            f"session {session_id} survived a daemon restart, so this platform "
            f"re-adopted its log but never its process (pid {pid}): the pid is no "
            "longer reserved by anything here and may since have been reused by an "
            "unrelated process, so it will not be signalled. Wind the session down "
            "instead — that reaches it over its control plane — or end pid "
            f"{pid} by hand if you mean to"
        )

    parent = _parent_pid(pid)
    if parent is None:
        # No /proc to ask. The entry's own claim is all that is left, which is
        # weaker (it is a writable file) but still refuses the case this exists to
        # catch: a session spawned by a process that has since exited.
        owner = entry.get("owner_pid")
        if owner != os.getpid():
            raise SessionNotTerminable(
                f"session {session_id} was started by another process "
                f"(owner_pid {owner!r}, this is {os.getpid()}), so its pid {pid} is "
                "not reserved by anything here and will not be signalled. Wind the "
                "session down instead, or end it by hand"
            )
    elif parent != os.getpid():
        raise SessionNotTerminable(
            f"session {session_id}'s process (pid {pid}) is not a child of this "
            f"platform — its parent is pid {parent}. The pid is therefore not "
            "reserved by anything here and may have been reused since the entry "
            "was written, so it will not be signalled. Wind the session down "
            "instead, or end it by hand"
        )
    return pid


def _alive(pid: int) -> bool:
    """Whether *pid* still names a live process.

    Through the registry's own liveness rule so the zombie handling is not written
    twice: the platform is the session's parent, so an exited-but-unreaped child
    still answers ``kill(pid, 0)`` and would keep an exit waiting out its full
    grace period for a process that is already dead.
    """
    return registry.is_live({"pid": pid})


def _signal_group(pid: int, sig: int) -> bool:
    """Signal *pid*'s group, or *pid* alone when it does not lead one.

    ``False`` means the process was already gone. Any other failure is logged and
    reported as delivered, so the caller falls through to its liveness check rather
    than escalating against a process it cannot signal anyway.
    """
    try:
        leads_group = os.getpgid(pid) == pid
    except ProcessLookupError:
        return False
    except OSError:
        leads_group = False
    try:
        if leads_group:
            os.killpg(pid, sig)
        else:
            # Reached when something spawned the session without a new session of
            # its own. Signalling the group would be signalling *our* group, which
            # includes this daemon.
            logger.warning(
                "platform_session_not_group_leader pid=%d — signalling the process "
                "alone; anything it started may outlive it", pid,
            )
            os.kill(pid, sig)
    except ProcessLookupError:
        return False
    except OSError as exc:
        logger.warning(
            "platform_session_signal_failed pid=%d signal=%s error=%s", pid, sig, exc
        )
    return True


def _wait_gone(pid: int, timeout: float) -> bool:
    """Poll until *pid* is gone or *timeout* elapses."""
    deadline = time.monotonic() + timeout
    while True:
        if not _alive(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_EXIT_POLL_SECONDS)


def exit_session(session_id: str) -> ExitReport:
    """End a session now, by signalling its process group. The agent gets nothing.

    The blunt verb, and the one an operator has to mean: nothing is committed,
    nothing is pushed, and whatever the container was holding — including a port
    somebody was about to look at — goes with it. :func:`wind_down` is the ordinary
    path precisely because this one is unrecoverable.

    Refuses (:class:`SessionNotTerminable`, 409) rather than signalling a pid this
    process does not own; see the module docstring. Raises
    :class:`TerminationFailed` when a session survives the whole ladder, leaving its
    registry entry alone so the fleet view goes on showing a session that is still
    running.

    On success the entry is removed. A signalled process never exits 0, so the
    watcher that reaps it (:func:`lmer_platform.spawn._watch`) keeps the entry as a
    crash signal — and a session we killed on request must not be reported as one
    that died. The PTY log is untouched: it is the record of everything the session
    did, and it outlives the container by design (spec D16).
    """
    entry = registry.read_session(session_id)
    if entry is None:
        raise SessionNotTerminable(
            f"session {session_id} has no registry entry on this host: it has "
            "already ended (its log is still readable), or it was never here"
        )
    if not registry.is_live(entry):
        raise SessionNotTerminable(
            f"session {session_id}'s process is already gone. Its entry is kept as "
            "the crash signal — acknowledge it with a prune rather than an exit"
        )
    pid = _signallable_pid(session_id, entry)

    # Before the first signal, so a daemon that dies mid-ladder still leaves the
    # evidence that this ending was asked for rather than suffered.
    append_event(
        "session_exit_requested",
        note=session_id,
        data={"session": session_id, "pid": pid},
    )

    sent: list = []
    gone = False
    for sig, grace in (
        (signal.SIGTERM, EXIT_GRACE_SECONDS),
        (signal.SIGKILL, EXIT_KILL_GRACE_SECONDS),
    ):
        if not _signal_group(pid, sig):
            # ProcessLookupError on the way in: it went between the liveness check
            # and the signal.
            gone = not _alive(pid)
            break
        sent.append(sig.name)
        if _wait_gone(pid, grace):
            gone = True
            break

    if not gone:
        logger.error(
            "platform_session_exit_failed id=%s pid=%s signals=%s — its entry is "
            "left alone so the fleet view keeps showing it",
            session_id, pid, ",".join(sent),
        )
        append_event(
            "session_exit_failed",
            note=session_id,
            data={"session": session_id, "pid": pid, "signals": sent},
        )
        raise TerminationFailed(
            f"session {session_id} (pid {pid}) did not end, and it was sent "
            f"{' then '.join(sent) or 'nothing'}. Something is holding the process; "
            "it is still listed as running, which is the truth"
        )

    removed = registry.remove(session_id)
    logger.info(
        "platform_session_exited_on_request id=%s pid=%s signals=%s removed=%s",
        session_id, pid, ",".join(sent), removed,
    )
    return ExitReport(
        session_id=session_id,
        pid=pid,
        at=utc_now_iso(),
        signals=tuple(sent),
        entry_removed=removed,
    )
