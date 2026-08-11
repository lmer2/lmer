"""``lmer-end-session`` — how an agent ends its own lmer session.

Why this is not part of the Slack integration
---------------------------------------------
It used to be. The only way for an agent to end its own session was
``lmer-slack end-session``, which is Slack-coupled and documented only in the
``chat`` taskdef's instructions. That was fine while chat was the only long-lived
session, and stopped being fine the moment the orchestrator grew a **wind down**
verb (spec §7.5 / D22): wind-down sends the agent a prompt asking it to land its
work and then end the session, and a ``develop`` run receiving that prompt had no
generic way to comply and no documentation telling it one existed. The platform
was offering an operator a button whose effect depended on the agent guessing.

Nothing about the mechanism was ever Slack-specific — only the optional goodbye
post was. So the mechanism lives here, ``lmer-slack end-session`` keeps its post
and delegates the rest, and any harness in any taskdef can end its own session.

How it works
------------
The in-container supervisor publishes its own PID in ``LMER_SUPERVISOR_PID``
before it forks the harness. ``SIGUSR1`` is its "the agent asked to leave" signal:
it injects the harness's quit chord, the harness exits cleanly, the ``lmer``
process exits 0, and the host reaper reads that exit status as a deliberate
sign-off rather than a crash — freeing the session slot immediately instead of
waiting out the idle timeout.

That exit status is the whole point, and it is why this is a signal to the
supervisor rather than the agent simply killing itself. A harness killed from the
inside looks exactly like a harness that died, and the platform would report a
session the agent ended cleanly as one that crashed.

One refusal: an answer the agent never read
-------------------------------------------
This command runs inside the container, in the same environment ``lmer-ask``
works in, so it can see the session's operator channel — and it is the last thing
an agent runs. That makes it the one place a mechanical check can catch the
failure prose could not: a session timed out ``lmer-ask wait``, was told to resume
it, worked for an hour, never waited again, and recorded "unanswered" while the
operator's reply sat on the channel.

So before signalling, the channel is read for answers that were never handed to
the agent (:func:`ask_channel.protocol.unread_answers` — the read receipt is the
discriminator). If there are any, this refuses **once**: it prints each question's
id and the full answer text, files the receipts, and exits
:data:`UNREAD_ANSWER_EXIT_CODE`. The refusal is the delivery, which is why it
records one, and why running the command again with nothing unread simply ends the
session. No flag turns it off and there is no state beyond the receipts.

An open question that nobody has answered does **not** block: ending a session
with a question outstanding is legitimate, and the run-level answer flow picks it
up after the session is gone.

And the check never wedges a shutdown. Every way of failing to read the channel —
no channel, a mount that is not there, an unreadable directory, receipts that
cannot be written — ends with the session ending, plus a warning line. A backstop
that could not record its own delivery would refuse every attempt forever, which
is a worse failure than the one it exists to prevent.

Exit codes
----------
``0``
    Shutdown requested; the supervisor was signalled.
``1``
    The attempt errored (the signal could not be delivered).
``3`` (:data:`NO_SUPERVISOR_EXIT_CODE`)
    There was no supervisor to signal — the variable is unset or invalid, or the
    process is gone. Deliberately distinct from ``1`` so a caller can tell
    "nothing to shut down" from "shutting down failed"; the difference matters to
    a wind-down, where the first means this session never had a supervisor and the
    second means one is there but unreachable.
``4`` (:data:`UNREAD_ANSWER_EXIT_CODE`)
    Refused: the operator answered a question and the answer had never been read.
    It is printed in the refusal, and the next attempt proceeds. Its own code
    because it is the one refusal the agent can clear by itself.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from typing import List, Optional

__all__ = [
    "SUPERVISOR_PID_ENV", "NO_SUPERVISOR_EXIT_CODE", "UNREAD_ANSWER_EXIT_CODE",
    "SessionEndResult", "supervisor_pid", "request_session_end", "main",
]

#: Where the in-container supervisor publishes its PID. Kept in step with
#: :data:`lmer_cli.supervisor.SUPERVISOR_PID_ENV` — same constant, and a test
#: asserts they have not drifted apart.
SUPERVISOR_PID_ENV = "LMER_SUPERVISOR_PID"

#: See the module docstring: distinct from 1 on purpose.
NO_SUPERVISOR_EXIT_CODE = 3

#: Refused because an operator's answer had never been read. Its own code so a
#: wrapper can tell "act on the answer and run me again" from a session that
#: could not be ended at all.
UNREAD_ANSWER_EXIT_CODE = 4

#: What the receipt records as the deliverer, so a later reader can tell an answer
#: the agent came back for from one this refusal put in front of it.
_READ_VIA = "end-session"


class SessionEndResult:
    """The outcome of a shutdown request, as data rather than as a printed line.

    A class rather than a bare exit code because two callers need different things
    from the same attempt: the CLI prints and exits, while ``lmer-slack
    end-session`` has already posted a goodbye and needs to report *its* own
    context around the same result.
    """

    def __init__(self, code: int, message: str, pid: Optional[int] = None) -> None:
        self.code = code
        self.message = message
        self.pid = pid

    @property
    def ok(self) -> bool:
        return self.code == 0


def supervisor_pid() -> Optional[int]:
    """The supervisor's PID from the environment, or ``None`` if unusable.

    ``None`` covers unset, empty, non-numeric and non-positive alike, because the
    caller's response to all four is identical: there is nothing to signal. A
    non-positive value is rejected rather than passed through — ``os.kill(0, …)``
    signals the caller's own process group, and ``os.kill(-1, …)`` signals every
    process the user owns, so a malformed variable must never reach it.
    """
    raw = os.environ.get(SUPERVISOR_PID_ENV, "").strip()
    if not raw:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def request_session_end() -> SessionEndResult:
    """Ask the supervisor to shut this session down cleanly.

    Never raises: every failure comes back as a result with a code, because the
    common caller is an agent winding down and an exception traceback is a worse
    thing to hand it than a sentence saying what happened.
    """
    pid = supervisor_pid()
    if pid is None:
        return SessionEndResult(
            NO_SUPERVISOR_EXIT_CODE,
            f"no supervisor to signal ({SUPERVISOR_PID_ENV} is unset or invalid). "
            "This session cannot shut itself down — it will end on the idle "
            "timeout instead.",
        )

    try:
        os.kill(pid, signal.SIGUSR1)
    except ProcessLookupError:
        return SessionEndResult(
            NO_SUPERVISOR_EXIT_CODE,
            f"supervisor process {pid} is gone; nothing to shut down.",
            pid=pid,
        )
    except OSError as exc:
        return SessionEndResult(1, f"could not signal supervisor {pid}: {exc}", pid=pid)

    return SessionEndResult(
        0, f"shutdown requested (signaled supervisor pid {pid}).", pid=pid
    )


def _warn(message: str) -> None:
    """One line on stderr. Never a raise — see the module docstring."""
    print(f"lmer-end-session: {message}", file=sys.stderr)


def _unread_answers():
    """``(protocol, directory, entries)`` for answers nobody has read.

    ``entries`` is empty on every kind of trouble, because this check protects an
    answer and a check that cannot run must not be what stops a session ending. A
    session that was never orchestrated says nothing (the ordinary case); anything
    else that stops the read gets a warning line.
    """
    try:
        from ask_channel import protocol
    except ImportError as exc:  # pragma: no cover - both ship in one distribution
        _warn(f"cannot check the operator channel for unread answers ({exc})")
        return None, None, []

    try:
        directory = protocol.resolve_channel_dir()
    except protocol.ChannelUnavailable as exc:
        if os.environ.get(protocol.ASK_DIR_ENV, "").strip():
            # The variable is set and the channel still could not be used: a
            # mount that is missing or that this uid cannot write. Worth saying,
            # because an answer may be sitting in it unseen.
            _warn(f"cannot check the operator channel for unread answers: {exc}")
        return protocol, None, []
    except Exception as exc:  # pragma: no cover - resolve_channel_dir is narrow
        _warn(f"cannot check the operator channel for unread answers ({exc})")
        return protocol, None, []

    try:
        return protocol, directory, protocol.unread_answers(directory)
    except Exception as exc:
        _warn(f"cannot read the operator channel ({exc}); ending the session anyway")
        return protocol, directory, []


def _refuse_for_unread_answers() -> bool:
    """Hand over answers nobody read; return whether to refuse this end-session.

    The printing is the point: this is the last thing the agent will be shown, so
    the answer itself goes in the refusal rather than a pointer to a command that
    would print it.
    """
    protocol, directory, unread = _unread_answers()
    if not unread:
        return False

    recorded = True
    for entry in unread:
        try:
            protocol.mark_answer_read(directory, entry, via=_READ_VIA)
        except Exception as exc:
            recorded = False
            _warn(f"could not record the delivery of answer {entry.id} ({exc})")

    _warn(
        f"the operator answered {_questions(len(unread))} you never read. "
        "Here is the reply — nothing else will show it to you:"
    )
    for entry in unread:
        print(f"\n  question {entry.id}: {entry.text}", file=sys.stderr)
        print(f"  answer: {entry.answer.text}", file=sys.stderr)

    if not recorded:
        # A refusal whose receipt did not land would refuse the next attempt too,
        # and the one after that. Delivery already happened above, so ending is
        # the honest outcome.
        _warn(
            "\nending the session anyway: the delivery could not be recorded, and "
            "a refusal that repeats forever is worse than one that is skipped"
        )
        return False

    print(
        "\nError: not ending this session yet. Act on that answer (or decide it "
        "changes nothing), then run lmer-end-session again — with nothing unread "
        "it proceeds.",
        file=sys.stderr,
    )
    return True


def _questions(count: int) -> str:
    return "1 question" if count == 1 else f"{count} questions"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lmer-end-session",
        description=(
            "End this lmer session cleanly: ask the in-container supervisor to "
            "quit the harness so the host frees the session slot immediately "
            "instead of waiting out the idle timeout. Land your work first — this "
            "does not wait for you. If the operator answered a question you never "
            "read, this refuses once and prints the answer."
        ),
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Say nothing on success (failures are still reported on stderr)",
    )
    args = parser.parse_args(argv)

    # Before the signal, never after: once the supervisor has the signal the
    # harness is on its way out and nothing printed is read by anybody.
    if _refuse_for_unread_answers():
        return UNREAD_ANSWER_EXIT_CODE

    result = request_session_end()
    if result.ok:
        if not args.quiet:
            print(result.message.capitalize())
    else:
        print(f"Error: {result.message}", file=sys.stderr)
    return result.code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
